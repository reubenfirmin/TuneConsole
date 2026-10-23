"""Confirmation contract for rating observations from the player bridge."""


def confirmed_rate(msg):
    status = msg.get("httpStatus")
    return (msg.get("phase") == "settled" and msg.get("success") is True
            and type(status) is int and 200 <= status < 300)
