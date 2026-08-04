"""Compatibility descriptors for extracted Binance gateway state."""

class _BookComponentAttribute:
    """Narrow compatibility descriptor for legacy gateway attributes."""

    def __init__(self, attribute: str, *, config: bool = False):
        self.attribute = attribute
        self.config = config

    def __get__(self, instance, owner):
        if instance is None:
            return self
        component = instance._order_books()
        target = component.config if self.config else component
        return getattr(target, self.attribute)

    def __set__(self, instance, value):
        component = instance._order_books()
        target = component.config if self.config else component
        setattr(target, self.attribute, value)


class _UserStreamComponentAttribute:
    """Narrow compatibility descriptor for listen-key lifecycle state."""

    def __init__(self, attribute: str):
        self.attribute = attribute

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return getattr(instance._user_streams(), self.attribute)

    def __set__(self, instance, value):
        setattr(instance._user_streams(), self.attribute, value)


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
    """Legacy gateway fields backed by their owning controllers."""

    orderbooks = _BookComponentAttribute("orderbooks")
    ws_buffer = _BookComponentAttribute("buffers")
    book_resyncing = _BookComponentAttribute("resyncing")
    book_recovery_generation = _BookComponentAttribute("recovery_generations")
    book_recovery_tokens = _BookComponentAttribute("recovery_tokens")
    _book_recovery_token = _BookComponentAttribute("recovery_token")
    _book_generation = _BookComponentAttribute("generation")
    _book_lock = _BookComponentAttribute("lock")
    _book_recovery_threads = _BookComponentAttribute("recovery_threads")
    _book_recovery_stop = _BookComponentAttribute("recovery_stop")

    max_book_buffer = _BookComponentAttribute("max_buffer", config=True)
    book_resync_max_attempts = _BookComponentAttribute(
        "resync_max_attempts",
        config=True,
    )
    book_resync_retry_sec = _BookComponentAttribute(
        "resync_retry_sec",
        config=True,
    )
    max_book_recovery_threads = _BookComponentAttribute(
        "max_recovery_threads",
        config=True,
    )
    book_recovery_join_timeout_sec = _BookComponentAttribute(
        "recovery_join_timeout_sec",
        config=True,
    )
    publish_depth_levels = _BookComponentAttribute(
        "publish_depth_levels",
        config=True,
    )
    emit_full_orderbook_events = _BookComponentAttribute(
        "emit_full_book",
        config=True,
    )
    max_orderbook_levels_per_side = _BookComponentAttribute(
        "max_levels_per_side",
        config=True,
    )
    max_delta_levels_per_side = _BookComponentAttribute(
        "max_delta_levels_per_side",
        config=True,
    )

    listen_key = _UserStreamComponentAttribute("listen_key")
    keep_alive_generation = _UserStreamComponentAttribute("generation")
    _keep_alive_stop = _UserStreamComponentAttribute("stop_event")
    _keep_alive_thread = _UserStreamComponentAttribute("thread")

    active = _ConnectionComponentAttribute("active")
    ws = _ConnectionComponentAttribute("ws")
    symbols = _ConnectionComponentAttribute("symbols")
    _closing = _ConnectionComponentAttribute("closing")
    recovery_lock = _ConnectionComponentAttribute("lifecycle_lock")

    target_leverage = _AccountConfigComponentAttribute("target_leverage")
    target_margin_type = _AccountConfigComponentAttribute(
        "target_margin_type"
    )
    target_position_mode = _AccountConfigComponentAttribute(
        "target_position_mode"
    )
    account_configuration_mode = _AccountConfigComponentAttribute("mode")

