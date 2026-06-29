from bias_ext_search.backend.extenders import (
    forum_extenders,
    frontend_extenders,
    route_extenders,
    service_extenders,
)


def extend():
    return [
        *frontend_extenders(),
        *route_extenders(),
        *forum_extenders(),
        *service_extenders(),
    ]
