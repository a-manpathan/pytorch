# Owner(s): ["module: dynamo"]
# flake8: noqa
import torch
import torch._dynamo

from torch._functorch.aot_autograd import aot_export_module
from torch._higher_order_ops.strict_mode import strict_mode

from torch.testing import FileCheck
from torch.testing._internal.common_utils import run_tests, TestCase


def _mark_strict_DO_NOT_USE(cls):
    def call(self, *args):
        return strict_mode(self, args)

    cls.__call__ = call
    return cls

class TestExperiment(TestCase):

    def test_with_buffer_as_submodule(self):
        @_mark_strict_DO_NOT_USE
        class B(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.register_buffer("buffer1", torch.ones(3))

            def forward(self, x):
                y = x + 2
                y.add_(4)
                self.buffer1.add_(6)
                return x.sum() + y.sum() + self.buffer1.sum()

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.submodule = B()

            def forward(self, x):
                x_v2 = x.sin()
                return (self.submodule(x_v2), x + 3)

        inp = torch.randn(3)
        gm, _ = aot_export_module(M(), (inp,), trace_joint=False)
        self.assertExpectedInline(str(gm.code.strip()), """\
def forward(self, arg0_1, arg1_1):
    sin = torch.ops.aten.sin.default(arg1_1)
    scalar_tensor = torch.ops.aten.scalar_tensor.default(True)
    _assert_async = torch.ops.aten._assert_async.msg(scalar_tensor, 'Input arg1_1.shape[0] is specialized at 3');  scalar_tensor = None
    scalar_tensor_1 = torch.ops.aten.scalar_tensor.default(True)
    _assert_async_1 = torch.ops.aten._assert_async.msg(scalar_tensor_1, 'Input arg1_1.shape[0] is specialized at 3');  scalar_tensor_1 = None
    add = torch.ops.aten.add.Tensor(sin, 2)
    add_1 = torch.ops.aten.add.Tensor(add, 4);  add = None
    add_2 = torch.ops.aten.add.Tensor(arg0_1, 6)
    sum_1 = torch.ops.aten.sum.default(sin);  sin = None
    sum_2 = torch.ops.aten.sum.default(add_1);  add_1 = None
    add_3 = torch.ops.aten.add.Tensor(sum_1, sum_2);  sum_1 = sum_2 = None
    sum_3 = torch.ops.aten.sum.default(add_2)
    add_4 = torch.ops.aten.add.Tensor(add_3, sum_3);  add_3 = sum_3 = None
    copy = torch.ops.aten.copy.default(arg0_1, add_2);  arg0_1 = add_2 = None
    add_5 = torch.ops.aten.add.Tensor(arg1_1, 3);  arg1_1 = None
    return (copy, add_4, add_5)""")

        eager_mod = M()

        graph_res_1, graph_res_2, graph_res_3 = gm(torch.ones(3), inp)
        eager_res_1, eager_res_2 = eager_mod(inp)

        self.assertTrue(torch.allclose(graph_res_2, eager_res_1))
        self.assertTrue(torch.allclose(graph_res_3, eager_res_2))

        graph_res_1, graph_res_2, graph_res_3 = gm(graph_res_1, inp)
        eager_res_1, eager_res_2 = eager_mod(inp)

        self.assertTrue(torch.allclose(graph_res_2, eager_res_1))
        self.assertTrue(torch.allclose(graph_res_3, eager_res_2))

    def test_cond(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, x):
                def true_fn(x):
                    return x.cos()

                def false_fn(x):
                    return x.sin()

                a = torch.cond(x.shape[0] > 4, true_fn, false_fn, [x])
                return (a + 3, a + 4)

        inp = torch.randn(3, 4)
        from torch.fx.experimental.proxy_tensor import make_fx
        gm, _ = aot_export_module(M(), (inp,), trace_joint=False)
        print(gm)



if __name__ == '__main__':
    run_tests()
