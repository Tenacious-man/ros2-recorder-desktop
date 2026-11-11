import io
import json

from ros2_recorder.lidar_monitor import Monitor, PAIR_WAIT_NS


def test_monitor_pairs_by_publish_time_and_reports_unmatched_frame_after_wait():
    output = io.StringIO()
    topic = '/points'
    monitor = Monitor(output, [(topic, 'eth0', 2368)], gap_ms=150, stall_ms=500)
    now = monitor.ready
    reliable, best_effort = (topic, 'reliable'), (topic, 'best_effort')

    def frame(key, elapsed_ms, published):
        monitor.frame(key, now + elapsed_ms * 1_000_000, 0, None,
                      {'source_timestamp': published}, 1)

    frame(reliable, 0, 10)
    frame(best_effort, 100, 10)
    frame(reliable, 100, 20)
    frame(best_effort, 300, 30)
    frame(reliable, 300, 30)
    monitor.check(now + PAIR_WAIT_NS)
    assert 'qos_difference' not in output.getvalue()
    monitor.check(now + PAIR_WAIT_NS + 100_000_000)

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    differences = [event for event in events if event['event'] == 'qos_difference']
    assert [(event['dds_source_ns'], event['observed_in']) for event in differences] == [(20, 'reliable')]
    summaries = {entry['mode']: entry for entry in monitor.summary()}
    assert summaries['reliable']['only_observed_here'] == 1
    assert summaries['best_effort']['only_observed_here'] == 0
    assert summaries['best_effort']['longest_gap_ms'] == 200
