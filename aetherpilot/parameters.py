"""Bounded arithmetic CDF evaluation; no Python eval, callbacks, or file access."""

import ast
import math

from .core.contract import engineering
from .errors import ValidationError


def resolve_parameter(name, parameters, parent_contexts=(), *, max_nodes=256, max_depth=32):
    inputs = []
    active = set()
    visited_nodes = 0

    def resolve(parameter, context, parents, depth, scope):
        if depth > max_depth or parameter not in context:
            raise ValueError("parameter reference is missing or exceeds its depth budget")
        identity = id(context), parameter
        if identity in active:
            raise ValueError("cyclic parameter expression")
        raw = context[parameter]
        try:
            return engineering(raw)
        except ValidationError:
            pass
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ValueError("parameter is not a bounded arithmetic expression")
        try:
            expression = ast.parse(raw, mode="eval")
        except (SyntaxError, ValueError) as exc:
            raise ValueError("unsupported parameter expression syntax") from exc
        active.add(identity)

        def evaluate(node):
            nonlocal visited_nodes
            visited_nodes += 1
            if visited_nodes > max_nodes:
                raise ValueError("parameter expression exceeds its node budget")
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                result = node.value
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                result = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                left, right = evaluate(node.left), evaluate(node.right)
                if isinstance(node.op, ast.Add):
                    result = left + right
                elif isinstance(node.op, ast.Sub):
                    result = left - right
                elif isinstance(node.op, ast.Mult):
                    result = left * right
                elif isinstance(node.op, ast.Div):
                    result = left / right
                else:
                    if abs(right) > 32:
                        raise ValueError("parameter exponent exceeds its budget")
                    result = left ** right
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"iPar", "pPar"} and not node.keywords and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                referenced = node.args[0].value
                if node.func.id == "pPar":
                    if not parents:
                        raise ValueError("parent parameter context is unavailable")
                    result = resolve(referenced, parents[0], parents[1:], depth + 1, "parent")
                else:
                    result = resolve(referenced, context, parents, depth + 1, scope)
                inputs.append({"scope": "parent" if node.func.id == "pPar" else scope, "parameter": referenced, "value": result})
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"abs", "sqrt", "min", "max"} and not node.keywords and 1 <= len(node.args) <= 16:
                arguments = [evaluate(argument) for argument in node.args]
                if node.func.id in {"abs", "sqrt"} and len(arguments) != 1:
                    raise ValueError("invalid arithmetic function argument count")
                function = {"abs": abs, "sqrt": math.sqrt, "min": min, "max": max}[node.func.id]
                result = function(arguments) if node.func.id in {"min", "max"} else function(arguments[0])
            else:
                raise ValueError("unsupported CDF expression node")
            if type(result) not in (int, float) or not math.isfinite(result) or abs(result) > 1e300:
                raise ValueError("parameter expression is not finite")
            return float(result)

        try:
            return evaluate(expression.body)
        except (ZeroDivisionError, OverflowError, TypeError) as exc:
            raise ValueError("invalid parameter arithmetic") from exc
        finally:
            active.remove(identity)

    value = resolve(name, parameters, list(parent_contexts), 0, "instance")
    return {"value": value, "expression": parameters[name], "inputs": inputs, "resolved": True,
            "evaluator": "bounded_arithmetic_ast_v1", "scope": "derived_from_native_parameter_literals"}
