"""Compatibility descriptors for externally configured Binance fields."""


class _ConnectionComponentAttribute:
    """Narrow compatibility descriptor for transport lifecycle state."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance._connections(), self.attribute)

    def __set__(self, instance, value):
        setattr(instance._connections(), self.attribute, value)


class _AccountConfigComponentAttribute:
    """Compatibility descriptor for account-mode configuration."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance._account_configuration(), self.attribute)

    def __set__(self, instance, value):
        setattr(instance._account_configuration(), self.attribute, value)


class BinanceGatewayCompatibilityFields:
    """Gateway facade fields still used by callers and OMS wiring."""

    active = _ConnectionComponentAttribute("active")
    symbols = _ConnectionComponentAttribute("symbols")

    target_leverage = _AccountConfigComponentAttribute("target_leverage")
    target_margin_type = _AccountConfigComponentAttribute(
        "target_margin_type"
    )
    target_position_mode = _AccountConfigComponentAttribute(
        "target_position_mode"
    )
    account_configuration_mode = _AccountConfigComponentAttribute("mode")
