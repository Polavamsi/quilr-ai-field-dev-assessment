from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

# [0-9] rather than \d: \d also matches non-ASCII digits such as "٣".
CustomerId = Annotated[str, StringConstraints(pattern=r"^CUST-[0-9]{5}$")]


def _at_most_two_decimals(value: float) -> float:
    if round(value, 2) != value:
        raise ValueError("amount must have at most 2 decimal places")
    return value


Amount = Annotated[
    float,
    Field(gt=0, le=10_000, allow_inf_nan=False),
    AfterValidator(_at_most_two_decimals),
]

# Whitespace is stripped before the length check, so ten spaces do not pass.
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)]


class _StrictArgs(BaseModel):
    # strict: no type coercion ("10" is not 10, true is not 1).
    # extra="forbid": unknown fields are an error, not silently dropped.
    model_config = ConfigDict(strict=True, extra="forbid")


class GetCustomerRecordArgs(_StrictArgs):
    customer_id: CustomerId


class TriggerRefundArgs(_StrictArgs):
    customer_id: CustomerId
    amount: Amount
    reason: Reason
