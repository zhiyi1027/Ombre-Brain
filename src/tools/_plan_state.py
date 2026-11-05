"""One derived location lifecycle for every active-plan reader."""

from clerk_locations import plan_state as local_state, plan_scope


def plan_reader(*, reference_time=None):
    from . import _runtime as rt

    clerk = getattr(getattr(rt, "daily_continuity", None), "clerk", None)
    method = getattr(clerk, "plan_location_state", None)
    snapshot = None

    def read(bucket):
        nonlocal snapshot
        initial = local_state(bucket, as_of=reference_time)
        if initial["effective_status"] != "active" or plan_scope(bucket) is None:
            return initial
        if callable(method):
            build = getattr(clerk, "location_snapshot", None)
            if callable(build):
                if snapshot is None:
                    snapshot = build()
                value = method(bucket, as_of=reference_time, snapshot=snapshot)
            else:
                value = method(bucket, as_of=reference_time)
            if isinstance(value, dict):
                return value
        return initial

    return read


def plan_state(bucket, *, reference_time=None):
    return plan_reader(reference_time=reference_time)(bucket)


def active_plan(bucket, *, reference_time=None, reader=None):
    return (reader or plan_reader(reference_time=reference_time))(bucket)["effective_status"] == "active"
