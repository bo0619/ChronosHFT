def handle_system_health_event(event, risk_controller):
    message = event.data
    if not isinstance(message, str):
        message = str(message)
    if message.startswith("HALT:"):
        return
    risk_controller.trigger_kill_switch(f"SystemHealth: {message}")
