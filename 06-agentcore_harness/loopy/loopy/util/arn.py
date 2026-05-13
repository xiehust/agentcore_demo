"""ARN utilities for the Loopy runtime."""


def region_from_arn(arn: str) -> str:
    """Extract the region from an ARN (arn:partition:service:region:account:resource)."""
    return arn.split(":")[3]


def resource_id_from_arn(arn: str) -> str:
    """Extract the resource ID from an ARN (the part after the last '/')."""
    return arn.rsplit("/", 1)[-1]
