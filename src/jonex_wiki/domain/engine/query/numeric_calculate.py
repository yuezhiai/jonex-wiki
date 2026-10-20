"""Deterministic calculation over evidence-bound operands.

``table_compute`` already aggregates one table's completed read.  This module is
the second, independent branch of the same tool: a short plan of arithmetic
steps over operands that bound to delivered evidence. It is deliberately a
separate parameter model - a cross-table calculation is not a single-table field
filter, and pretending otherwise is how a wrong number gets a right-looking
program result.

The rules the plan fixes are implemented here and nowhere else:

* add / subtract / multiply / divide / mean / compare, plus a conversion inside
  one verifiable scale family;
* a later step may reference an earlier step's result (``$1``), at most 16 steps,
  never a forward or self reference, never an expression string, never SQL;
* ``Decimal`` arithmetic; division at 28 significant digits; ``ROUND_HALF_UP``
  only when a display precision is asked for, never between steps;
* with no display precision the normalized decimal is kept - a computed 2.20 is
  shown as 2.2, not re-rounded by a financial convention;
* divide-by-zero, an unknown unit, a conversion that would need an external
  exchange rate and a missing operand are each a named error;
* a constant is only the exact text of the question or a fixed rule of an
  operation (percent = 100), never a coefficient the model supplies;
* a percentage keeps the value the source wrote (36.8% is 36.8 with unit %), so
  a difference between two shares is measured in percentage points; used as a
  factor in a multiplication or division the program converts it to its
  fraction itself;
* every step inherits the evidence handles of its operands, and the payload says
  which interpretations (metric, period, unit, subject scope) were never proven.
"""
from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      model_validator)

from jonex_wiki.domain.engine.query.numeric_operands import (
    CURRENCY_UNITS,
    PERCENT_UNITS,
    OperandError,
    OperandRef,
    ResolvedOperand,
    format_decimal,
    resolve_operand,
    scale_factor,
)

from .numeric_units import unit_spec, CURRENCIES

CALCULATE_RULE_VERSION = "calculate-v3"
CALCULATE_MAX_STEPS = 16
DIVISION_PRECISION = 28
CALCULATE_OPERATIONS = ("add", "subtract", "multiply", "divide", "mean",
                        "compare", "convert")
# Operations whose operands must agree on one unit before they can be combined.
SAME_UNIT_OPERATIONS = ("add", "subtract", "mean", "compare")


class CalculateOperand(BaseModel):
    """One operand: an earlier result, or an evidence-bound number."""

    result: Optional[str] = None
    operand: Optional[OperandRef] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _one_form(self) -> "CalculateOperand":
        if (self.result is None) == (self.operand is None):
            raise ValueError("CALCULATE_OPERAND_FORM_INVALID")
        if self.result is not None and not str(self.result).startswith("$"):
            raise ValueError("CALCULATE_RESULT_REFERENCE_INVALID")
        return self


class CalculateStep(BaseModel):
    step_id: str
    operation: str
    operands: List[CalculateOperand] = Field(min_length=1)
    display_decimals: Optional[int] = Field(default=None, ge=0, le=12)
    unit: Optional[str] = None
    label: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _known_operation(self) -> "CalculateStep":
        if self.operation not in CALCULATE_OPERATIONS:
            raise ValueError("CALCULATE_OPERATION_UNSUPPORTED:" + str(self.operation))
        if self.operation == "subtract" and len(self.operands) < 2:
            raise ValueError("CALCULATE_SUBTRACT_NEEDS_AT_LEAST_TWO_OPERANDS")
        if self.operation in ("divide", "compare") and len(self.operands) != 2:
            raise ValueError("CALCULATE_" + self.operation.upper() + "_NEEDS_TWO_OPERANDS")
        if self.operation == "convert":
            if len(self.operands) != 1 or not (self.unit or "").strip():
                raise ValueError("CALCULATE_CONVERT_NEEDS_ONE_OPERAND_AND_A_UNIT")
        return self


class CalculatePlan(BaseModel):
    steps: List[CalculateStep] = Field(min_length=1, max_length=CALCULATE_MAX_STEPS)
    label: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "CalculatePlan":
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("CALCULATE_STEP_ID_DUPLICATED")
        for index, step in enumerate(self.steps):
            for operand in step.operands:
                if operand.result is None:
                    continue
                name = str(operand.result)[1:]
                if not name.isdigit() and name in ids:
                    operand.result = "$" + str(ids.index(name) + 1)
                position = _result_position(str(operand.result))
                if position is None or position >= index:
                    # A reference to this step or to a later one is a cycle.
                    raise ValueError("CALCULATE_RESULT_REFERENCE_INVALID:"
                                     + str(operand.result))
        return self


def _result_position(reference: str) -> Optional[int]:
    body = reference[1:]
    if not body.isdigit():
        return None
    position = int(body) - 1
    return position if position >= 0 else None


class _StepValue:
    """One step's value plus everything the run can say about it."""

    def __init__(self, value: Optional[Decimal], unit: Optional[str],
                 handles: Tuple[str, ...], missing: Tuple[str, ...],
                 operands: Sequence[ResolvedOperand] = (),
                 label: Optional[str] = None) -> None:
        self.value = value
        self.unit = unit
        self.handles = handles
        self.missing = missing
        self.operands = tuple(operands)
        self.label = label


def _unit_ok(unit_a: Optional[str], unit_b: Optional[str]) -> bool:
    if unit_a and unit_b:
        return unit_a == unit_b
    return True


def _convert(value: Decimal, source: Optional[str], target: str) -> Decimal:
    left, right = unit_spec(source), unit_spec(target)
    if left is None or right is None:
        raise _Refusal("CALCULATE_UNIT_UNKNOWN:" + str(source if left is None else target))
    if left[0] != right[0]:
        if left[0] in CURRENCIES and right[0] in CURRENCIES:
            raise _Refusal("CALCULATE_FX_REQUIRED")
        if not source:
            raise _Refusal("CALCULATE_UNIT_UNKNOWN:" + str(target))
        raise _Refusal("CALCULATE_CONVERSION_UNVERIFIED")
    return value * left[1] / right[1]


def _is_percent(unit: Optional[str]) -> bool:
    spec = unit_spec(unit)
    return spec is not None and spec[0] == "%"


class _Refusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _decimal_sum(values: Sequence[Decimal]) -> Decimal:
    total = Decimal("0")
    for value in values:
        total += value
    return total


def _apply(step: CalculateStep, operands: Sequence[_StepValue],
           *, question: str, state: Mapping[str, Any]) -> Dict[str, Any]:
    operation = step.operation
    values = [item.value for item in operands]
    if any(value is None for value in values):
        raise _Refusal("CALCULATE_MISSING_OPERAND")
    numbers = [value for value in values if value is not None]
    units = [item.unit for item in operands]
    specs = [unit_spec(unit) for unit in units]
    if any(spec is None for spec in specs):
        raise _Refusal("CALCULATE_UNIT_UNKNOWN")
    # Convert explicit scales to their base before combining quantities. The
    # convert operation itself retains the requested display unit below.
    if operation != "convert":
        numbers = [n * spec[1] for n, spec in zip(numbers, specs)]
    bases = [spec[0] for spec in specs]
    handles: List[str] = []
    for item in operands:
        for handle in item.handles:
            if handle not in handles:
                handles.append(handle)
    missing: List[str] = []
    for item in operands:
        for key in item.missing:
            if key not in missing:
                missing.append(key)
    # A table often states currency on one row and omits it on the next.
    # Same-table arithmetic can still check its written numbers, without
    # inventing a unit or scale. Keep missing-unit semantics explicit; do not
    # mix a known quantity with an ungrounded constant or another table.
    source_operands = [source for item in operands for source in item.operands]
    same_table = (bool(source_operands)
                  and all(source.kind == "table_cell" for source in source_operands)
                  and len({source.table_id for source in source_operands}) == 1)
    known_bases = {base for base in bases if base}
    if ("unit" in missing and len(known_bases) <= 1
            and (operation == "divide" or (operation in SAME_UNIT_OPERATIONS and same_table))):
        # A previous result may already be in base currency while a missing-unit
        # input is still a written figure. Never report their mixed-scale ratio.
        if any(item.unit and unit_spec(item.unit)[1] == 1
               and any(source.unit and unit_spec(source.unit)[1] != 1
                       for source in item.operands) for item in operands):
            raise _Refusal("CALCULATE_UNIT_UNVERIFIED")
        numbers = list(values)
        bases = [None] * len(operands)
    unit = (bases[0] or None) if bases else None
    if operation in SAME_UNIT_OPERATIONS:
        nonempty = {base for base in bases if base}
        if nonempty and any(not base for base in bases):
            raise _Refusal("CALCULATE_UNIT_UNVERIFIED")
        if len(nonempty) > 1:
            if nonempty <= CURRENCIES:
                raise _Refusal("CALCULATE_FX_REQUIRED")
            raise _Refusal("CALCULATE_UNIT_MISMATCH")
    if operation in ("multiply", "divide"):
        numbers = [n * Decimal("0.01") if base == "%" else n
                   for n, base in zip(numbers, bases)]
        powers: Dict[str, int] = {}
        for index, base in enumerate(bases):
            if base and base != "%":
                exponent = -1 if operation == "divide" and index else 1
                powers[base] = powers.get(base, 0) + exponent
        powers = {base: exponent for base, exponent in powers.items() if exponent}
        # Support scalar factors, same-unit cancellation and one carried unit.
        # Compound dimensions are refused instead of mislabeled as one input.
        if len(powers) > 1 or any(exponent != 1 for exponent in powers.values()):
            raise _Refusal("CALCULATE_UNIT_UNSUPPORTED")
        unit = next(iter(powers), None)

    with localcontext() as context:
        context.prec = DIVISION_PRECISION
        if operation == "add":
            value = _decimal_sum(numbers)
        elif operation == "subtract":
            value = numbers[0]
            for other in numbers[1:]:
                value -= other
        elif operation == "multiply":
            value = Decimal("1")
            for number in numbers:
                value *= number
        elif operation == "divide":
            value = numbers[0]
            for other in numbers[1:]:
                if other == 0:
                    raise _Refusal("CALCULATE_DIVIDE_BY_ZERO")
                value /= other
        elif operation == "mean":
            value = _decimal_sum(numbers) / Decimal(len(numbers))
        elif operation == "convert":
            value = _convert(numbers[0], operands[0].unit, str(step.unit))
            unit = str(step.unit)
        elif operation == "compare":
            base, current = numbers[0], numbers[1]
            value, _written_unit, extra = _compare(base, current, operands)
            unit = bases[0] or None
            return {**extra, "value": value, "unit": unit, "handles": handles,
                    "missing": missing, "operands": [source for item in operands for source in item.operands]}
        else:  # pragma: no cover - the parameter model refuses anything else
            raise _Refusal("CALCULATE_OPERATION_UNSUPPORTED:" + str(operation))
    return {"value": value, "unit": unit, "handles": handles, "missing": missing,
            "operands": [source for item in operands for source in item.operands]}


def _compare(base: Decimal, current: Decimal, operands: Sequence[_StepValue]
             ) -> Tuple[Decimal, Optional[str], Dict[str, Any]]:
    """Direction, absolute change and both relative measures, named apart.

    Percentage-point change and relative percentage change are different
    quantities; the program reports each under its own key.  ``改善``/``恶化`` is
    not derived here at all: a numeric sign says 上升/下降, and whether that is an
    improvement is a judgement the evidence has to make.
    """
    change = current - base
    direction = "上升" if change > 0 else ("下降" if change < 0 else "持平")
    percentage_points = None
    relative_percent = None
    if base != 0:
        relative_percent = (change / base) * Decimal("100")
    if _is_percent(operands[0].unit) or _is_percent(operands[1].unit):
        # Both operands are written in percent, so their difference *is* the
        # change in percentage points - a different quantity from the relative
        # percentage change below, and reported under its own key.
        percentage_points = change
    extra = {
        "direction": direction,
        "base_value": base,
        "current_value": current,
        "absolute_change": change,
        "percentage_points": percentage_points,
        "relative_percent": relative_percent,
        "base_label": operands[0].label if operands else None,
        "relative_to": operands[0].label if operands else None,
        "improvement": None,
        "note": ("方向由数值符号给出（上升/下降/持平）；改善或恶化是业务判断，"
                 "本结果不推得，需要另行依据。百分点变化与相对百分比变化分别列在 "
                 "percentage_points 与 relative_percent。"),
    }
    return change, operands[0].unit, extra


def execute_calculate(raw_plan: Union[CalculatePlan, Mapping[str, Any]], *,
                      state: Mapping[str, Any], question: str = "") -> Dict[str, Any]:
    """Execute a calculation plan over operands the run already delivered."""
    if isinstance(raw_plan, CalculatePlan):
        plan = raw_plan
    else:
        plan = CalculatePlan.model_validate(raw_plan)
    results: List[Dict[str, Any]] = []
    carried: List[_StepValue] = []
    for index, step in enumerate(plan.steps):
        try:
            operands = [_resolve_step_operand(operand, carried, state=state,
                                              question=question)
                        for operand in step.operands]
            outcome = _apply(step, operands, question=question, state=state)
        except OperandError as error:
            return _refused(step, "CALCULATE_OPERAND_UNRESOLVED:" + str(error), results)
        except _Refusal as refusal:
            return _refused(step, refusal.reason, results)
        except (DivisionByZero, InvalidOperation, ZeroDivisionError):
            return _refused(step, "CALCULATE_DIVIDE_BY_ZERO", results)
        record = {
            "step_id": step.step_id,
            "operation": step.operation,
            "unit": outcome.get("unit"),
            "display_decimals": step.display_decimals,
            "exact": format_decimal(outcome["value"]) if outcome.get("value") is not None
            else None,
            "display": (format_decimal(outcome["value"], step.display_decimals)
                        if outcome.get("value") is not None else None),
            "evidence_handles": list(outcome["handles"]),
            "missing": list(outcome["missing"]),
            "operands": [operand.as_payload() for operand in outcome.get("operands", ())
                         if isinstance(operand, ResolvedOperand)],
        }
        for key in ("direction", "base_value", "current_value", "absolute_change",
                    "percentage_points", "relative_percent", "relative_to", "note",
                    "improvement"):
            if key in outcome:
                value = outcome[key]
                if isinstance(value, Decimal):
                    record[key] = format_decimal(value)
                    if key in ("percentage_points", "relative_percent"):
                        # Display precision only: the exact value stays available.
                        record[key + "_exact"] = format_decimal(value)
                        record[key] = format_decimal(value, 2)
                else:
                    record[key] = value
        results.append(record)
        carried.append(_StepValue(
            outcome.get("value"), outcome.get("unit"), tuple(outcome["handles"]),
            tuple(outcome["missing"]), operands=outcome.get("operands") or (),
            label=step.label or "第{}步结果".format(index + 1)))
    last = results[-1]
    handles = _all_handles(results)
    missing = sorted({key for record in results for key in record["missing"]})
    return {
        "status": "complete",
        "operation": "calculate",
        "rule_version": CALCULATE_RULE_VERSION,
        "steps": results,
        "result": last.get("display"),
        "exact_result": last.get("exact"),
        "unit": last.get("unit"),
        "display_decimals": last.get("display_decimals"),
        "comparison": _comparison_of(last),
        "unproven": missing,
        "citations": handles,
        "rendered": _render_calculation(results),
        "note": ("Deterministic calculation over operands bound to delivered evidence; "
                 "every step lists the [E*] handles it inherited. Interpretations listed "
                 "in ``unproven`` were never proven by this run and must not be assumed."),
    }


def _comparison_of(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if record.get("operation") != "compare":
        return None
    # ``improvement`` is kept even when it is null: the program states that it
    # did not derive 改善/恶化, rather than leaving the field out.
    return {key: record.get(key) for key in
            ("direction", "base_value", "current_value", "absolute_change",
             "percentage_points", "relative_percent", "relative_to", "unit",
             "improvement")}


def _all_handles(results: Sequence[Mapping[str, Any]]) -> List[str]:
    handles: List[str] = []
    for record in results:
        for handle in record.get("evidence_handles") or []:
            if handle not in handles:
                handles.append(handle)
    return handles


def _render_calculation(results: Sequence[Mapping[str, Any]]) -> str:
    parts: List[str] = []
    for record in results:
        if record.get("operation") == "compare":
            direction = record.get("direction")
            absolute = record.get("absolute_change")
            unit = record.get("unit") or ""
            base = record.get("relative_to") or "未标明"
            if record.get("percentage_points") is not None:
                text = "{}{}个百分点（基期 {}）".format(
                    direction, record["percentage_points"].lstrip("-"), base)
            else:
                text = "{}{}（基期 {}）".format(
                    direction, _with_unit(absolute.lstrip("-"), unit), base)
            if record.get("relative_percent") is not None:
                text += "，相对变化{}%".format(record["relative_percent"])
            parts.append(text)
            continue
        parts.append(_with_unit(record.get("display"), record.get("unit")))
    return "；".join(parts) + "。" if parts else ""


def _with_unit(value: Optional[str], unit: Optional[str]) -> str:
    if value is None:
        return "无值"
    return "{}{}".format(value, unit) if unit else str(value)


def _resolve_step_operand(operand: CalculateOperand, carried: Sequence[_StepValue],
                          *, state: Mapping[str, Any], question: str) -> _StepValue:
    if operand.result is not None:
        position = _result_position(str(operand.result))
        if position is None or position >= len(carried):
            raise _Refusal("CALCULATE_RESULT_REFERENCE_INVALID:" + str(operand.result))
        previous = carried[position]
        return _StepValue(previous.value, previous.unit, previous.handles,
                          previous.missing, operands=previous.operands,
                          label=previous.label)
    assert operand.operand is not None
    resolved = resolve_operand(operand.operand, state, question=question)
    return _StepValue(resolved.value, resolved.unit, resolved.evidence_handles,
                      resolved.missing, operands=(resolved,),
                      label=operand.operand.label or resolved.label)


def _refused(step: CalculateStep, reason: str,
             results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "status": "rejected",
        "correctable": True,
        "operation": "calculate",
        "rule_version": CALCULATE_RULE_VERSION,
        "reason": reason,
        "failed_step_id": step.step_id,
        "steps": list(results),
        "citations": _all_handles(results),
        "note": ("The calculation stopped at this step; no value is reported for it. "
                 "Fix the named operand, unit or reference and call again, or answer "
                 "without the computation and state what is missing."),
    }


def evaluate_operands(plan: CalculatePlan, *, state: Mapping[str, Any],
                      question: str = "") -> Tuple[List[ResolvedOperand], List[str]]:
    """Resolve every evidence operand of a plan, for callers that only check."""
    resolved: List[ResolvedOperand] = []
    problems: List[str] = []
    for step in plan.steps:
        for operand in step.operands:
            if operand.operand is None:
                continue
            try:
                resolved.append(resolve_operand(operand.operand, state,
                                                question=question))
            except OperandError as error:
                problems.append("{}:{}".format(step.step_id, error))
    return resolved, problems


def parse_plan(raw: Any) -> Tuple[Optional[CalculatePlan], Optional[str]]:
    """Validate a plan without executing it; the reason names what was wrong."""
    try:
        return CalculatePlan.model_validate(raw), None
    except ValidationError as error:
        first = (error.errors() or [{}])[0]
        message = str(first.get("msg") or "").replace("Value error, ", "")
        location = ".".join(str(part) for part in first.get("loc", ()))
        return None, (message or str(error).splitlines()[0]) + (
            " at " + location if location else "")
    except (ValueError, TypeError) as error:
        return None, str(error).splitlines()[0]


__all__ = ["CALCULATE_MAX_STEPS", "CALCULATE_OPERATIONS", "CALCULATE_RULE_VERSION",
           "DIVISION_PRECISION", "CalculateOperand", "CalculatePlan", "CalculateStep",
           "evaluate_operands", "execute_calculate", "parse_plan"]
