from django.dispatch import Signal

# Sent once per client per window when the limit is first exceeded.
# providing_args: request, info
traffic_exceeded = Signal()
