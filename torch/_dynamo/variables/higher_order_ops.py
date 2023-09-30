import contextlib
import functools
import itertools
import logging

from typing import Dict, List, Optional, Tuple

import torch._C
import torch.fx
import torch.nn
import torch.onnx.operators
from torch._dispatch.python import enable_python_dispatcher
from torch._dynamo.utils import deepcopy_to_fake_tensor, get_fake_value, get_real_value
from torch._dynamo.variables.base import VariableTracker
from torch._dynamo.variables.builtin import BuiltinVariable
from torch._dynamo.variables.functions import UserFunctionVariable
from torch._dynamo.variables.tensor import SymNodeVariable
from torch._guards import Source
from torch.utils import _pytree as pytree

from ..exc import UncapturedHigherOrderOpError, unimplemented, Unsupported
from ..guards import GuardBuilder
from ..source import FSDPNNModuleSource, GetItemSource, NNModuleSource
from ..utils import proxy_args_kwargs
from .dicts import ConstDictVariable
from .lists import ListVariable, TupleVariable
from .nn_module import NNModuleVariable


log = logging.getLogger(__name__)


def safe_or_raise_always_restore(tx, graph_checkpoint, checkpoint, f, sub_args):
    # Will raise if not sound
    try:
        f.call_function(tx, sub_args, {})
    finally:
        tx.output.graph = graph_checkpoint
        tx.restore_graphstate(checkpoint)


def raise_hard_error_if_graph_break(reason):
    def deco(fn):
        @functools.wraps(fn)
        def graph_break_as_hard_error(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Unsupported as e:
                msg = " Scroll up to find out what causes the graph break."
                raise UncapturedHigherOrderOpError(reason + msg) from e

        return graph_break_as_hard_error

    return deco


@contextlib.contextmanager
def dynamo_enable_grad(tx):
    from . import GradModeVariable

    org_value = torch.is_grad_enabled()
    try:
        GradModeVariable.create(tx, True)
        yield
    finally:
        GradModeVariable.create(tx, org_value)


def only_consist_of(var, types):
    if isinstance(var, types):
        return True
    if isinstance(var, (TupleVariable, ListVariable)):
        return all(only_consist_of(item, types) for item in var.items)
    if isinstance(var, ConstDictVariable):
        return all(only_consist_of(item, types) for item in var.items.values())
    return False


def tree_flatten(tx, variable) -> Tuple[VariableTracker, VariableTracker]:
    return tuple(
        UserFunctionVariable(pytree.tree_flatten)
        .call_function(tx, [variable], {})
        .unpack_var_sequence(tx)
    )


def tree_unflatten(tx, variable, treespec) -> VariableTracker:
    return UserFunctionVariable(pytree.tree_unflatten).call_function(
        tx, [variable, treespec], {}
    )


def tree_map(tx, function, variable) -> VariableTracker:
    return UserFunctionVariable(pytree.tree_map).call_function(
        tx, [function, variable], {}
    )


def validate_args_and_maybe_create_graph_inputs(
    sub_args, tracer, tx, manually_set_subgraph_inputs
):
    from . import (
        AutogradFunctionContextVariable,
        ConstantVariable,
        SymNodeVariable,
        TensorVariable,
    )
    from .builder import wrap_fx_proxy, wrap_fx_proxy_cls

    assert tracer.parent is not None

    args = []
    for a in sub_args:
        assert isinstance(a, VariableTracker)

        if isinstance(a, ConstantVariable):
            # Ensures that we recompile when the constant value changes
            a.add_guard(GuardBuilder.CONSTANT_MATCH)

            if manually_set_subgraph_inputs:
                # This arg is not used in the body of the higher order op.
                # Currently, this new input is added to make the calls
                # happy, which expect a fixed number of arguments. In
                # future, we can clean this up.
                tracer.create_graph_input("const")
            new_arg = a
        elif isinstance(a, TensorVariable):
            if manually_set_subgraph_inputs:
                new_proxy = tracer.create_graph_input(a.as_proxy().node.name)
                example_value = a.as_proxy().node.meta["example_value"]
                new_arg = wrap_fx_proxy(
                    tx=tx, proxy=new_proxy, example_value=example_value
                )
            else:
                new_arg = a
        elif isinstance(a, SymNodeVariable):
            if manually_set_subgraph_inputs:
                new_proxy = tracer.create_graph_input(str(a.sym_num.node.expr))
                new_arg = wrap_fx_proxy_cls(
                    target_cls=SymNodeVariable,
                    tx=tx,
                    proxy=new_proxy,
                    example_value=a.sym_num,
                )
            else:
                new_arg = a
        elif isinstance(a, AutogradFunctionContextVariable):
            if manually_set_subgraph_inputs:
                tracer.create_graph_input(a.as_proxy().node.name)
            new_arg = a
        else:
            if manually_set_subgraph_inputs:
                raise unimplemented(
                    f"HigherOrderOperator with body that accepts non-Tensors as input. "
                    f"Got: {a.python_type()}"
                )
            else:
                # leverage tracer's lifting mechanism to lift these args.
                if only_consist_of(
                    a, (ConstantVariable, SymNodeVariable, TensorVariable)
                ):
                    new_arg = a
                else:
                    unimplemented(
                        "HigherOrderOperator with body that accepts non-Tensors as input that can't be lifted by tracer."
                    )

        args.append(new_arg)
    return args


# See NOTE [HigherOrderOperator tracing design] for details of the design
def speculate_subgraph(
    tx,
    f,
    sub_args,
    sub_kwargs,
    graph_checkpoint,
    checkpoint,
    description,
    *,
    always_restore=False,
    enable_grad=False,
    # NOTE [Temporary argument `manually_set_subgraph_inputs`]
    # If manually_set_subgraph_inputs=True, then we manually add
    # the `sub_args` to `subgraph`, if False then we rely
    # on tracer's lifting mechanism to lift these args.
    # NOTE: Default `True` is temporary and plan is
    #       to always lift args in future and remove this
    #       argument.
    manually_set_subgraph_inputs=True,
    restore_side_effects=True,
    should_flatten_inputs=False,
    should_flatten_outputs=False,
):
    if sub_kwargs is None:
        sub_kwargs = {}

    # See NOTE [Temporary argument `manually_set_subgraph_inputs`]
    if sub_kwargs and manually_set_subgraph_inputs:
        unimplemented(
            "Use `manually_set_subgraph_inputs=False` when passing `sub_kwargs`."
        )

    varlist, _treespec = tree_flatten(tx, ListVariable(list(sub_args)))
    varlist = varlist.unpack_var_sequence(tx)

    try:
        with tx.output.new_subtracer() as tracer:
            args = validate_args_and_maybe_create_graph_inputs(
                varlist, tracer, tx, manually_set_subgraph_inputs
            )

            if not should_flatten_inputs:
                args = tree_unflatten(
                    tx, ListVariable(args), _treespec
                ).unpack_var_sequence(tx)

            validate_args_and_maybe_create_graph_inputs(
                sub_kwargs.values(), tracer, tx, manually_set_subgraph_inputs=False
            )

            autograd_ctx = (
                dynamo_enable_grad(tx) if enable_grad else contextlib.nullcontext()
            )

            if restore_side_effects:
                prev_side_effects = tx.output.side_effects.clone()

            with autograd_ctx:
                output = f.call_function(tx, args, sub_kwargs)

            if restore_side_effects:
                # Captured variables are tracked in side-effects
                # and they show up in output graph incorrectly.
                # It is ok to undo this side-effect tracking
                # as speculate_subgraph will allow only
                # pure functions.
                tx.output.side_effects = prev_side_effects

            treespec = None
            if should_flatten_outputs:
                # Flatten the speculated subgraph output.
                output, treespec = tree_flatten(tx, output)
                # Actually, transform the list (returned by flatten) into a tuple
                # for dynamo consistency.
                output = BuiltinVariable(tuple).call_function(tx, [output], {})

            # Register output to graph
            # Modeled off of compile_and_call_fx_graph
            # TODO: support pytree output
            # We check always_restore because we dont use the output or side effects of always_restore code,
            # like bwd.
            if always_restore:
                # Nothing left to do here
                return (output, treespec), tx.output.graph, tracer.lifted_freevars
            else:
                from . import TensorVariable

                if not only_consist_of(output, TensorVariable):
                    unimplemented(
                        "HigherOrderOperator body's output must consist of tensors only"
                    )

                tx.output.guards.update(output.guards)
                # The output proxies might not belong to this SubgraphTracer
                # (if they are free variables that were never lifted)
                # so lift them here.
                output_proxies = output.as_proxy()
                output_proxies = pytree.tree_map(
                    tracer.maybe_lift_tracked_freevar_to_input, output_proxies
                )
                tx.output.create_node(
                    "output",
                    "output",
                    (tracer.create_arg((output_proxies,))),
                    {},
                )
                graph = tx.output.graph
                graph.lint()
                lifted_freevars = tracer.lifted_freevars

                return (
                    (output, treespec),
                    graph,
                    lifted_freevars,
                )

    except Unsupported as ex:
        msg = (
            f"speculate_subgraph: while introspecting {description}, we were unable "
            f"to trace function `{f.get_name()}` into a single graph. This means "
            f"that Dynamo was unable to prove safety for this API and will "
            f"fall back to eager-mode PyTorch, which could lead to a slowdown."
        )
        log.warning(msg)
        log.exception(ex)
        tx.output.graph = graph_checkpoint
        tx.restore_graphstate(checkpoint)
        raise Unsupported(
            f"{msg} Scroll up for the stack trace "
            f"of the initial exception. The reason was: {ex.msg}"
        ) from ex


def make_attr(tx, name):
    node = tx.output.create_proxy(
        "get_attr",
        name,
        (),
        {},
    )
    return node


def add_subgraph(tx, source, name, gm):
    next_name = None
    i = 0
    while not next_name:
        candidate = f"{name}_{i}"
        if candidate in tx.output.nn_modules:
            i += 1
        else:
            next_name = candidate

    gm.__name__ = next_name
    if source.guard_source().is_fsdp_module():
        src = FSDPNNModuleSource(GetItemSource(source, next_name))
    else:
        src = NNModuleSource(GetItemSource(source, next_name))
    gm.torchdynamo_force_dynamic = False
    tx.output.register_attr_or_module(gm, next_name, source=src)
    return next_name


class TorchHigherOrderOperatorVariable(VariableTracker):
    def __init__(self, value, source: Optional[Source] = None, **kwargs):
        super().__init__(**kwargs)
        self.value = value
        self.source = source

    @staticmethod
    def make(value, source=None, **kwargs):
        if value.__name__ == "cond":
            return CondHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ == "map":
            return MapHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ == "executorch_call_delegate":
            return ExecutorchCallDelegateHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ == "out_dtype":
            return OutDtypeHigherOrderVariable(value, source, **kwargs)
        elif value is torch._functorch.eager_transforms.grad_impl:
            return FunctorchGradHigherOrderVariable(value, source, **kwargs)
        elif value is torch._functorch.vmap.vmap_impl:
            return FunctorchVmapHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ in (
            "trampoline_autograd_fwd",
            "trampoline_autograd_bwd",
            "trampoline_autograd_apply",
        ):
            return AutogradFunctionMethodHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ == "wrap":
            return WrapHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ in (
            "wrap_activation_checkpoint",
            "tag_activation_checkpoint",
        ):
            return CheckpointHigherOrderVariable(value, source, **kwargs)
        elif value.__name__ == "_export_tracepoint":
            return ExportTracepointHigherOrderVariable(value, source, **kwargs)
        else:
            unimplemented(f"HigherOrderOperator {value.__name__}")

    def check_kwargs(self, kwargs, supported_types):
        if not all(isinstance(value, supported_types) for value in kwargs.values()):
            raise unimplemented(
                f"Only kwargs of the following types are supported: {supported_types}"
            )

    def call_function(
        self, tx, args: List[VariableTracker], kwargs: Dict[str, VariableTracker]
    ) -> VariableTracker:
        unimplemented(f"HigherOrderOperator {self.value.__name__}")


class CondHigherOrderVariable(TorchHigherOrderOperatorVariable):
    @raise_hard_error_if_graph_break(
        reason="Cond doesn't work unless it is captured completely with torch.compile."
    )
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import (
            ConstantVariable,
            ListVariable,
            NestedUserFunctionVariable,
            TensorVariable,
            UserFunctionVariable,
        )
        from .builder import wrap_fx_proxy

        # TODO(voz): Support fake tensor dispatch for recursive
        # ops - see torch/dispatch/_dispatcher.py
        if len(args) != 4:
            unimplemented(
                f"Expected 4 arguments but got {len(args)}.\n"
                f"Usage: cond(pred, true_fn, false_fn, operands)",
            )
        # predicate
        if type(args[0]) not in (ConstantVariable, TensorVariable, SymNodeVariable):
            unimplemented(
                f"Expected pred to be bool or a boolean tensor with single "
                f"item but got {str(type(args[0]))} "
                f"with original python type {str(args[0].python_type())}.",
            )
        tx.output.guards.update(args[0].guards)

        # operands
        if not isinstance(args[3], (ListVariable, TupleVariable)):
            unimplemented(
                f"Expected a tuple but got {args[3].python_type()}",
            )
        operands = args[3].unpack_var_sequence(tx)
        if not all(
            isinstance(operand, (TensorVariable, torch.Tensor)) for operand in operands
        ):
            unimplemented(
                "Expected a tuple of tensors but got {actual_args}".format(  # noqa: UP032
                    actual_args=[
                        str(operand.python_type())
                        if isinstance(operand, VariableTracker)
                        else str(type(operand))
                        for operand in operands
                    ],
                ),
            )

        # branches
        assert isinstance(
            args[1],
            (UserFunctionVariable, NestedUserFunctionVariable, NNModuleVariable),
        ), str(
            type(args[1])
        )  # true_fn

        assert isinstance(
            args[2],
            (UserFunctionVariable, NestedUserFunctionVariable, NNModuleVariable),
        ), str(
            type(args[2])
        )  # false_fn

        # Our strategy for tracing the true/false branches of cond
        # are to checkpoint our graphstate, run the true branch,
        # roll it back to the checkpoint, and run the false
        # branch, and then merge the graphstates.  Well, perhaps
        # "merge" is too strong a word: we mostly assert that
        # the resulting graphstates have to be the same.
        #
        # We only permit guards to diverge (we union the guards from
        # both branches).  In particular, this means that side
        # effects are NOT permitted inside true/false branches; this
        # would be difficult to implement, because of the path
        # explosion problem.

        graph_checkpoint, checkpoint = tx.output.graph, tx.copy_graphstate()

        def speculate_branch(branch):
            # NB: 0 is predicate
            ix = 1 if branch else 2
            # TODO: Support kwargs
            (ret_val, _), ret_graph, ret_lifted_freevars = speculate_subgraph(
                tx,
                args[ix],
                operands,
                {},
                graph_checkpoint,
                checkpoint,
                "cond",
            )

            if not isinstance(ret_val, TensorVariable):
                unimplemented(
                    "Expected branch to return a single tensor",
                )
            return ret_val, ret_graph, ret_lifted_freevars

        (true_r, true_graph, true_lifted_freevars) = speculate_branch(True)
        true_nn_modules = tx.copy_graphstate().output.nn_modules

        (false_r, false_graph, false_lifted_freevars) = speculate_branch(False)
        false_nn_modules = tx.copy_graphstate().output.nn_modules

        # TODO (tmanlaibaatar) deduplicate this later
        # Let's say we capture cond(pred, true_fn, false_fn, x)
        # and true_fn has lifted variables a, b, c
        # and false_fn has lifted variables a, b, d
        # Then each branch graph will receive:
        # true_fn(x, a, b, c, a_false, b_false, d_false)
        # false_fn(x, a_true, b_true, c_true, a, b, d)
        # https://github.com/pytorch/pytorch/issues/103530
        def fixup_branch_inps(graph, add_after, new_args, suffix) -> None:
            original_phs = [node for node in graph.nodes if node.op == "placeholder"]
            assert add_after < len(
                original_phs
            ), f"Invalid index for inserting lifted arguments {add_after}."

            # When operands is empty, add_after can be -1 for false graph. In that case, we need to insert new
            # nodes before the first node in the graph since placeholders precede normal nodes.
            def _add_phs():
                for inp_node in new_args:
                    new_node_name = inp_node.node.name + suffix
                    graph.placeholder(new_node_name)

            if add_after == -1:
                first_node = next(iter(graph.nodes))
                with graph.inserting_before(first_node):
                    _add_phs()
            else:
                insertion_node = original_phs[add_after]
                with graph.inserting_after(insertion_node):
                    _add_phs()

        fixup_branch_inps(
            true_graph,
            len(operands) + len(true_lifted_freevars) - 1,
            false_lifted_freevars,
            "_false_branch",
        )

        fixup_branch_inps(
            false_graph, len(operands) - 1, true_lifted_freevars, "_true_branch"
        )

        true_name = add_subgraph(
            tx,
            self.source,
            "cond_true",
            torch.fx.GraphModule(true_nn_modules.nn_modules, true_graph),
        )
        false_name = add_subgraph(
            tx,
            self.source,
            "cond_false",
            torch.fx.GraphModule(false_nn_modules.nn_modules, false_graph),
        )

        true_node = make_attr(tx, true_name)
        false_node = make_attr(tx, false_name)

        p_args = (
            args[0].as_proxy(),
            true_node,
            false_node,
            [a.as_proxy() for a in operands]
            + list(true_lifted_freevars.keys())
            + list(false_lifted_freevars.keys()),
        )
        # TODO: assert that the true/false return values are
        # consistent
        example_value = true_r.as_proxy().node.meta["example_value"]

        _, p_kwargs = proxy_args_kwargs([], kwargs)

        # Store the invocation as a call
        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                torch.ops.higher_order.cond,
                args=tuple(p_args),
                kwargs=p_kwargs,
            ),
            example_value=example_value,
        )


def non_single_tensor_return_unsupported(api, ret):
    from . import TensorVariable

    if not isinstance(ret, TensorVariable):
        raise Unsupported(
            f"{api} over function that returns something " f"other than one Tensor"
        )


class MapHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: List[VariableTracker], kwargs: Dict[str, VariableTracker]
    ) -> VariableTracker:
        from . import (
            ConstantVariable,
            NestedUserFunctionVariable,
            TensorVariable,
            UserFunctionVariable,
        )
        from .builder import wrap_fx_proxy

        assert type(args[0]) in (UserFunctionVariable, NestedUserFunctionVariable)
        assert type(args[1]) is TensorVariable

        sample_shape = args[1].get_real_value().size()
        if len(sample_shape) < 1 or sample_shape[0] == 0:
            unimplemented(
                "map() operator doesn't support scalar or zero-sized tensors during tracing."
            )

        checkpoint = tx.copy_graphstate()
        # To get the example output from map() we will need to provide at least one sample to
        # the loop body. In our case we will always use xs[0], and our map() won't support zero
        # sized tensor during tracing.
        first_dim = args[1].call_method(
            tx, "__getitem__", args=[ConstantVariable.create(0)], kwargs={}
        )

        # TODO: Support kwargs
        (
            (body_r, _),
            body_graph,
            body_lifted_freevars,
        ) = speculate_subgraph(
            tx,
            args[0],
            [
                first_dim,
                *args[2:],
            ],
            {},
            tx.output.graph,
            checkpoint,
            "torch.ops.higher_order.map",
        )

        body_nn_modules = tx.copy_graphstate().output.nn_modules

        body_name = add_subgraph(
            tx,
            self.source,
            "map_body",
            torch.fx.GraphModule(body_nn_modules.nn_modules, body_graph),
        )

        body_node = make_attr(tx, body_name)
        p_args = (
            body_node,
            *(arg.as_proxy() for arg in args[1:]),
            *(arg for arg in body_lifted_freevars.keys()),
        )
        non_single_tensor_return_unsupported("torch.ops.higher_order.map", body_r)
        r = body_r.as_proxy().node.meta["example_value"]
        example_value = r.new_empty(
            [get_fake_value(args[1].as_proxy().node, tx).shape[0], *r.shape]
        )

        _, p_kwargs = proxy_args_kwargs([], kwargs)

        # Store the invocation as a call
        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs=p_kwargs,
            ),
            example_value=example_value,
        )


class ExecutorchCallDelegateHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import ConstantVariable
        from .builder import wrap_fx_proxy

        self.check_kwargs(kwargs, ConstantVariable)

        # This is operator for delegation within Executorch which calls a
        # specific function in the given lowered module with the given
        # operators. The actual operator is defined in the Executorch codebase.
        # This is a bad hierarchical violation since
        # executorch_call_delegate sits at a higher level than dynamo, but
        # there's no real solution to this issue yet.
        lowered_module = tx.output.get_submodule(args[0].module_key)

        lowered_node = make_attr(tx, args[0].module_key)

        p_args = tuple(arg.as_proxy() for arg in args[1:])
        real_sub_args = pytree.tree_map_only(
            torch.fx.Proxy, lambda a: get_real_value(a.node, tx.output), p_args
        )
        example_res = lowered_module.original_module(*real_sub_args)
        example_value = deepcopy_to_fake_tensor(example_res, tx.fake_mode)

        p_args = (lowered_node,) + p_args

        _, p_kwargs = proxy_args_kwargs([], kwargs)

        # Store the invocation as a call
        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs=p_kwargs,
            ),
            example_value=example_value,
        )


class FunctorchGradHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import ConstantVariable
        from .builder import wrap_fx_proxy

        # TODO: Support `fn` with kwargs.
        if not torch._dynamo.config.capture_func_transforms:
            unimplemented(
                "torch.func.grad capture is disabled, "
                "it can be turned on by setting "
                "`torch._dynamo.config.capture_func_transforms=True`"
            )
        # [NOTE] Here we are (roughly) modelling the following
        #
        #   grad_fn = torch.func.grad(fn, argnums=.., has_aux=..)
        #   grad_output = grad_fn(x)
        checkpoint = tx.copy_graphstate()
        graph_checkpoint = tx.output.graph
        grad_args = (args[0], args[1], args[2])

        # get arguments
        func, raw_variable_argnums, has_aux = grad_args
        kwargs = args[4].items
        if len(kwargs) > 0:
            # Since speculate_subgraph doesn't support kwargs, we can't handle this for now.
            unimplemented(
                "torch.func.grad: kwargs arguments are currently unsupported."
            )

        raw_argnums = raw_variable_argnums.as_python_constant()
        assert isinstance(raw_argnums, int) or (
            isinstance(raw_argnums, tuple)
            and all(isinstance(i, int) for i in raw_argnums)
        ), f"argnums is expected to be int or tuple of ints. Got: {raw_argnums}"

        grouped_flat_args = [
            tree_flatten(tx, a)[0] for a in args[3].unpack_var_sequence(tx)
        ]
        raw_argnums_tuple = (
            (raw_argnums,) if isinstance(raw_argnums, int) else raw_argnums
        )
        flat_argnums = []

        base = 0
        for i, flat_args in enumerate(grouped_flat_args):
            if i in raw_argnums_tuple:
                flat_argnums.extend(range(base, base + len(flat_args.items)))
            base += len(flat_args.items)

        if isinstance(raw_argnums, int) and len(flat_argnums) == 1:
            flat_variable_argnums = ConstantVariable(raw_argnums)
        else:
            flat_variable_argnums = TupleVariable(
                [ConstantVariable(i) for i in flat_argnums]
            )

        # Trace through the `func`
        # NOTE [HACK: Enable autograd while tracing function]
        # `torch.func.grad` should not be affected by `no_grad` outside of `grad`.
        # So, we enable_grad right before the function to which `grad` is applied
        # (the parts explicitly disabled with `no_grad` inside the function are still disabled).
        # Eg.
        # def f(x):
        #     with no_grad():  # This will disable grad tracking under it.
        #        y = x * 2
        #
        #     return x ** 2 - y  # grad tracking should be enabled irrespective of outside `no_grad`.
        #
        # with no_grad():  # This will not disable grad tracking inside of grad(f).
        #     grad_o = torch.func.grad(f)(x)
        # TODO: Support kwargs
        (body_r, _), body_graph, body_lifted_freevars = speculate_subgraph(
            tx,
            func,
            args[3].items,
            {},
            graph_checkpoint,
            checkpoint,
            "torch.func.grad",
            # See NOTE [HACK: Enable autograd while tracing function]
            enable_grad=True,
        )

        body_name = add_subgraph(
            tx,
            self.source,
            "grad_body",
            torch.fx.GraphModule(tx.output.nn_modules, body_graph),
        )
        body_node = make_attr(tx, body_name)

        grad_proxy_args = (
            body_node,
            flat_variable_argnums.as_proxy(),
            has_aux.as_proxy(),
        )

        # Model `grad_fn = grad(fn, *grad_args, **grad_kwargs)`
        grad_fn = tx.output.create_proxy(
            "call_function",
            torch.func.grad,
            args=tuple(grad_proxy_args),
            kwargs={},
            name="grad_proxy",
        )

        # Pass lifted freevars to the call to `grad_fn`
        args = args[3].items
        flat_args = [
            a for arg in grouped_flat_args for a in arg.unpack_var_sequence(tx)
        ]
        grad_fn_args = tuple(arg.as_proxy() for arg in flat_args) + tuple(
            body_lifted_freevars
        )

        # Call grad_fn with inputs.
        # grad_output = grad_fn(*grad_fn_args, **grad_fn_kwargs)
        grad_output = grad_fn(*grad_fn_args)

        # `grad_fn(*grad_fn_args, **grad_fn_kwargs)`
        # Output of grad_fn is
        # For has_aux=False, Tuple[gradients of inputs indicated by argnums].
        # For has_aux=True, Tuple[Tuple[gradients of inputs indicated by argnums], aux values]
        # NOTE: example_value should match `grad_output`.
        example_value = tuple(
            flat_args[i].as_proxy().node.meta["example_value"] for i in flat_argnums
        )
        if len(example_value) == 1:
            example_value = example_value[0]

        if has_aux.value:
            # case : has_aux = True
            # NOTE: Currently speculate subgraph allows body_r to be
            # Tensor or Tuple/List of Tensor.
            # Since `grad` expects output with has_aux
            # to be (output, aux), only valid output currently is
            # (output, some_tensor)
            body_r_proxy = body_r.as_proxy()
            aux = body_r_proxy[1].node.meta["example_value"]
            example_value = (example_value, aux)

        fx_proxy = wrap_fx_proxy(tx=tx, proxy=grad_output, example_value=example_value)

        def call_contiguous(variable):
            return variable.call_method(tx, "contiguous", (), {})

        # Call contiguous on all the computed grads.
        if not has_aux.value and len(flat_argnums) == 1:
            return call_contiguous(fx_proxy)
        elif not has_aux.value and len(flat_argnums) > 1:
            return TupleVariable(
                [call_contiguous(v) for v in fx_proxy.unpack_var_sequence(tx)]
            )

        grads, aux = fx_proxy.unpack_var_sequence(tx)
        if len(flat_argnums) == 1:
            return TupleVariable([call_contiguous(grads), aux])
        else:
            grads_variable = TupleVariable(
                [call_contiguous(v) for v in grads.unpack_var_sequence(tx)]
            )
            return TupleVariable([grads_variable, aux])


class FunctorchVmapHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import ConstantVariable, TensorVariable
        from .builder import wrap_fx_proxy

        if not torch._dynamo.config.capture_func_transforms:
            unimplemented(
                "torch.func.vmap capture is disabled, "
                "it can be turned on by setting "
                "`torch._dynamo.config.capture_func_transforms=True`"
            )

        checkpoint = tx.copy_graphstate()
        graph_checkpoint = tx.output.graph

        # unpack args
        fn = args[0]
        in_dims = args[1]
        out_dims = args[2]
        randomness = args[3]
        chunk_size = args[4]
        batch_input_args = args[5:]

        if not isinstance(in_dims, (ConstantVariable, TupleVariable)):
            unimplemented("torch.func.vmap: in_dims is not an int or tuple variable.")

        if not isinstance(out_dims, (ConstantVariable, TupleVariable)):
            unimplemented("torch.func.vmap: out_dims is not an int or tuple variable.")

        if kwargs:
            unimplemented(
                "NYI - torch.func.vmap: kwargs arguments are currently unsupported."
            )

        if chunk_size.value is not None:
            unimplemented(
                "NYI - torch.func.vmap is not implemented when chunk_size is passed"
            )

        # Trace into tree_flatten with the list of batch_input_args.
        tree_flatten = UserFunctionVariable(pytree.tree_flatten)
        flat_args, arg_spec = tree_flatten.call_function(
            tx, [ListVariable(batch_input_args)], {}
        ).unpack_var_sequence(tx)

        # Transform in_dims into a list if it's not an integer literal.
        in_dims_v = (
            in_dims
            if isinstance(in_dims.as_python_constant(), int)
            else BuiltinVariable(list).call_function(tx, [in_dims], {})
        )

        # Trace into broadcast_to_and_flatten with the transformed in_dims.
        broadcast_to_and_flatten = UserFunctionVariable(
            pytree._broadcast_to_and_flatten
        )
        broadcasted_in_dims = broadcast_to_and_flatten.call_function(
            tx, [in_dims_v, arg_spec], {}
        )

        # We want to pass unbatched input to speculate subgraph.
        # So we loop through the inputs and select only one sample
        # from the batch.
        unbatched_input_args = []
        for arg, in_dim in zip(
            flat_args.unpack_var_sequence(tx),
            broadcasted_in_dims.unpack_var_sequence(tx),
        ):
            if in_dim is not None:
                assert isinstance(arg, TensorVariable)
                unbatched_arg = arg.call_method(
                    tx, "select", [in_dim, ConstantVariable.create(0)], {}
                )
                unbatched_input_args.append(unbatched_arg)
            else:
                unbatched_input_args.append(arg)

        # Ban ops like `stride`, `storage_offset` in the traced functions.
        # NOTE: We are conservatively banning more ops (vmap should be able
        #       to handle a few of them).
        with tx.strict_translation_mode():
            # trace through the function with unbatched inputs.
            _, body_graph, body_lifted_freevars = speculate_subgraph(
                tx,
                fn,
                # Returns a ListVariable, since that's where we started flattening.
                # However, we really want to pass the inner Python list as argument.
                tree_unflatten(
                    tx, ListVariable(unbatched_input_args), arg_spec
                ).unpack_var_sequence(tx),
                {},
                graph_checkpoint,
                checkpoint,
                "torch.vmap",
            )

        body_name = add_subgraph(
            tx,
            self.source,
            "vmap_body",
            torch.fx.GraphModule(tx.output.nn_modules, body_graph),
        )
        body_node = make_attr(tx, body_name)

        # body_lifted_variable should not be treated as batched.
        # So here we update `in_dims` to reflect that.
        # NOTE: updated_in_dims is flat list, it is ok for now
        #       as speculate_subgraph does not supports functions with non-Tensor args.
        #       (so we graph-break above)
        updated_in_dims = TupleVariable(
            broadcasted_in_dims.unpack_var_sequence(tx)
            + [
                ConstantVariable.create(None),
            ]
            * len(body_lifted_freevars)
        )

        vmap_proxy_args = (
            body_node,
            *(arg.as_proxy() for arg in (updated_in_dims, out_dims, randomness)),
        )
        # vmap_proxy corresponds to `vmap_proxy = vmap(fn, *vmap_args, **vmap_kwargs)`
        vmap_proxy = tx.output.create_proxy(
            "call_function",
            torch.func.vmap,
            args=tuple(vmap_proxy_args),
            kwargs={},
            name="vmap_proxy",
        )

        proxy_batched_fn_args = tuple(
            arg.as_proxy() for arg in batch_input_args
        ) + tuple(body_lifted_freevars)

        # We compute the example_value by actually calling
        # `vmap` with FakeTensors.
        if not all(isinstance(a, TensorVariable) for a in batch_input_args):
            types = [type(a).__name__ for a in batch_input_args]
            unimplemented(f"calling vmap with unsupported arg types: {types}")

        fake_batched_fn_args = itertools.chain(
            (get_fake_value(arg.as_proxy().node, tx) for arg in batch_input_args),
            (get_fake_value(arg.node, tx) for arg in body_lifted_freevars),
        )
        actual_in_dims = tuple(
            pytree.tree_map(lambda x: x.value, updated_in_dims.items)
        )

        # NOTE: `body_graph` might have operators which
        # will create new tensors. So it is required
        # that we run `vmap` under FakeMode.
        with tx.fake_mode, enable_python_dispatcher():
            example_value = torch._functorch.vmap.vmap_impl(
                torch.fx.GraphModule(tx.output.nn_modules, body_graph),
                actual_in_dims,
                out_dims.as_python_constant(),
                randomness.value,
                chunk_size.value,
                *fake_batched_fn_args,
            )

        # proxy corresponds to `call = vmap_proxy(*batched_fn_args, **batched_fn_kwargs)`
        proxy = vmap_proxy(*proxy_batched_fn_args)
        return wrap_fx_proxy(
            tx=tx,
            proxy=proxy,
            example_value=example_value,
        )


class AutogradFunctionMethodHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from . import ConstantVariable, UserFunctionVariable
        from .builder import wrap_fx_proxy

        self.check_kwargs(kwargs, ConstantVariable)

        from . import TorchVariable

        always_restore = self.value.__name__ == "trampoline_autograd_bwd"
        if (
            self.value.__name__ == "trampoline_autograd_bwd"
            or self.value.__name__ == "trampoline_autograd_fwd"
        ):
            fn = UserFunctionVariable(self.value, source=self.source)
        else:
            fn = TorchVariable(self.value)
        checkpoint = tx.copy_graphstate()
        pre_guards = tx.output.guards
        graph_checkpoint = tx.output.graph

        # TODO: Support kwargs
        (
            (body_r, treespec),
            body_graph,
            body_lifted_freevars,
        ) = speculate_subgraph(
            tx,
            fn,
            [
                *args,
            ],
            {},
            graph_checkpoint,
            checkpoint,
            "the user-defined autograd.Function",
            # Backwards should never, ever be stored!
            always_restore=always_restore,
            restore_side_effects=False,
            should_flatten_inputs=True,
            should_flatten_outputs=True,
        )
        post_guards = tx.output.guards
        if body_lifted_freevars:
            for freevar in body_lifted_freevars.keys():
                if "saved_tensor_marked" not in freevar.node.meta:
                    unimplemented("NYI - freevars in autograd function.")

        if always_restore:
            if post_guards - pre_guards:
                unimplemented("NYI - New guards discovered in a restoring state")
            # Nothing left to do here
            return None

        p_args = (
            *(arg.as_proxy() for arg in args),
            *(arg for arg in body_lifted_freevars.keys()),
        )
        # non_single_tensor_return_unsupported("autograd.Function forward", body_r)
        example_value = pytree.tree_map_only(
            torch.fx.Proxy,
            lambda a: a.node.meta["example_value"],
            body_r.as_proxy(),
        )

        _, p_kwargs = proxy_args_kwargs([], kwargs)

        # Store the invocation as a call
        variable = wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs=p_kwargs,
            ),
            example_value=example_value,
        )

        if treespec is None:
            return variable

        # Transform variable back into a list (previously made into a tuple by
        # speculate_subgraph function) so as to respect the pytree API typing.
        variable = BuiltinVariable(list).call_function(tx, [variable], {})

        return tree_unflatten(tx, variable, treespec)


class WrapHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def create_wrapped_node(self, tx, args, kwargs, description):
        # See NOTE [HigherOrderOperator tracing design] for more details
        checkpoint = tx.copy_graphstate()
        graph_checkpoint = tx.output.graph

        (
            (body_r, treespec),
            body_graph,
            body_lifted_freevars,
        ) = speculate_subgraph(
            tx,
            args[0],  # function
            [*args[1:]],
            kwargs,
            graph_checkpoint,
            checkpoint,
            description,
            manually_set_subgraph_inputs=False,
            should_flatten_outputs=True,
        )

        body_name = add_subgraph(
            tx,
            self.source,
            "wrap_body",
            torch.fx.GraphModule(tx.output.nn_modules, body_graph),
        )

        body_node = make_attr(tx, body_name)

        # Since, we call `speculate_subgraph` with `manually_set_subgraph_inputs=False`,
        # all the arguments are lifted.
        lifted_args = tuple(arg for arg in body_lifted_freevars.keys())

        proxy_args = (body_node,) + lifted_args
        example_value = pytree.tree_map_only(
            torch.fx.Proxy,
            lambda a: a.node.meta["example_value"],
            body_r.as_proxy(),
        )

        return proxy_args, {}, example_value, treespec

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from .builder import wrap_fx_proxy

        p_args, p_kwargs, example_value, treespec = self.create_wrapped_node(
            tx, args, kwargs, "wrap"
        )

        # Store the invocation as a call
        variable = wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs=p_kwargs,
            ),
            example_value=example_value,
        )

        if treespec is None:
            return variable

        # Transform variable back into a list (previously made into a tuple by
        # speculate_subgraph function) so as to respect the pytree API typing.
        variable = BuiltinVariable(list).call_function(tx, [variable], {})

        return tree_unflatten(tx, variable, treespec)


class OutDtypeHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from .builder import wrap_fx_proxy

        if len(kwargs) != 0:
            unimplemented("out_dtype does not handle kwargs")

        p_args = tuple(arg.as_proxy() for arg in args)
        op = p_args[0]
        output_dtype = p_args[1]
        fake_sub_args = pytree.tree_map_only(
            torch.fx.Proxy, lambda a: a.node.meta["example_value"], p_args[2:]
        )
        # This is a simplified implementation of this operator just for tracing.
        # Actual implementation may also first promote the arguments
        example_value = op(*fake_sub_args).to(dtype=output_dtype)

        # Store the invocation as a call
        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs={},
            ),
            example_value=example_value,
        )


class CheckpointHigherOrderVariable(WrapHigherOrderVariable):
    def call_function(
        self, tx, args: List[VariableTracker], kwargs: Dict[str, VariableTracker]
    ) -> VariableTracker:
        from torch._higher_order_ops.wrap import TagActivationCheckpoint
        from torch.utils.checkpoint import noop_context_fn
        from .builder import wrap_fx_proxy

        if "context_fn" in kwargs and kwargs["context_fn"] != noop_context_fn:
            context_fn = kwargs.pop("context_fn")
            self.value.context_fn = context_fn.fn

        checkpoint_kwargs, gmod_kwargs = TagActivationCheckpoint.divide_kwargs(kwargs)

        # Here we use checkpoint_kwargs (and not gmod kwargs). gmod_kwargs are
        # already flattened above and managed inside the fx graph.
        p_args, _, example_value, treespec = self.create_wrapped_node(
            tx, args, gmod_kwargs, "torch.utils.checkpoint.checkpoint"
        )

        _, checkpoint_kwargs = proxy_args_kwargs([], checkpoint_kwargs)

        # Store the invocation as a call
        variable = wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=tuple(p_args),
                kwargs=checkpoint_kwargs,
            ),
            example_value=example_value,
        )

        if treespec is None:
            return variable

        # Transform variable back into a list (previously made into a tuple by
        # speculate_subgraph function) so as to respect the pytree API typing.
        variable = BuiltinVariable(list).call_function(tx, [variable], {})

        return tree_unflatten(tx, variable, treespec)


class ExportTracepointHigherOrderVariable(TorchHigherOrderOperatorVariable):
    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        from .builder import wrap_fx_proxy

        p_args = tuple(arg.as_proxy() for arg in args)
        p_kwargs = {key: arg.as_proxy() for key, arg in kwargs.items()}
        return wrap_fx_proxy(
            tx=tx,
            proxy=tx.output.create_proxy(
                "call_function",
                self.value,
                args=p_args,
                kwargs=p_kwargs,
            ),
            example_value=None,
        )
