"""Enhanced tests for security module — list/dict/subscript support."""

import pytest

from neoagent.core.security import get_safe_evaluator


class TestSafeEvaluatorEnhanced:
    @pytest.fixture
    def evaluator(self):
        return get_safe_evaluator()

    def test_list_indexing(self, evaluator):
        assert evaluator.evaluate("[1, 2, 3][0]") == 1
        assert evaluator.evaluate("[10, 20, 30][-1]") == 30

    def test_tuple_indexing(self, evaluator):
        assert evaluator.evaluate("(5, 6, 7)[1]") == 6

    def test_dict_lookup(self, evaluator):
        assert evaluator.evaluate('{"a": 1, "b": 2}["a"]') == 1

    def test_nested_containers(self, evaluator):
        assert evaluator.evaluate("[[1, 2], [3, 4]][1][0]") == 3

    def test_list_literal(self, evaluator):
        assert evaluator.evaluate("[1, 2, 3]") == [1, 2, 3]

    def test_dict_literal(self, evaluator):
        assert evaluator.evaluate('{"x": 10}') == {"x": 10}

    def test_slice(self, evaluator):
        assert evaluator.evaluate("[1, 2, 3, 4][1:3]") == [2, 3]

    def test_slice_with_step(self, evaluator):
        assert evaluator.evaluate("[1, 2, 3, 4][::2]") == [1, 3]
