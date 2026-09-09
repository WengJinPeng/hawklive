"""Connection evidence is separate from cloud heartbeats and measurement freshness."""
from __future__ import annotations


def device_snapshot(devices, latest, *, running, poll_seconds, now):
    readings = {str(item.get('device_id')): item for item in latest}
    states = []
    for device in devices:
        item = readings.get(str(device['id']), {})
        checked = float(item.get('connection_checked_at') or item.get('timestamp') or 0)
        # A failed check remains valid through its scheduled reconnect backoff.
        valid_until = max(checked + max(45, float(poll_seconds) * 2.5),
                          float(item.get('retry_at') or 0) + 45)
        state = 'unknown'
        if running and checked > 0 and checked <= now + 5 and now <= valid_until:
            if item.get('error') and item.get('online') is False:
                state = 'offline'
            elif item.get('source') == 'device' and item.get('online') is True:
                state = 'online'
        states.append({'device_id': str(device['id']), 'state': state, 'checked_at': checked or None})
    return {'device_states': states, 'device_total': len(states),
            **{f'device_{state}': sum(item['state'] == state for item in states)
               for state in ('online', 'offline', 'unknown')}}


def cloud_device_status(status, connected):
    """Expired heartbeats cannot provide current device connection evidence."""
    total = int(status.get('device_total') or 0)
    known = connected and status.get('monitor_running') is True
    states = status.get('device_states')
    if isinstance(states, list):
        states = [{**item, 'state': item['state'] if known else 'unknown'} for item in states]
        counts = {f'device_{state}': sum(item['state'] == state for item in states)
                  for state in ('online', 'offline', 'unknown')}
    else:
        # Older collectors report only successful reads. Remaining devices are
        # unconfirmed: zero successful reads is not evidence of failed checks.
        online = int(status.get('device_online') or 0) if known else 0
        counts = {'device_online': online, 'device_offline': 0, 'device_unknown': total - online}
    return {'device_states': states, 'device_total': total, **counts,
            'device_status_known': bool(known),
            'last_reported_device_online': int(status.get('device_online') or 0)}
