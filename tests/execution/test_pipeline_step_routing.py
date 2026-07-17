import execution.pipeline_orchestrator as po


def test_data_alerts_helper_targets_data_alerts(monkeypatch):
    calls = []
    monkeypatch.setattr(po, 'post_channel', lambda ch, msg: calls.append((ch, msg)) or True)
    po.data_alerts('hello')
    assert calls == [('data-alerts', 'hello')]


def test_pipeline_feed_still_targets_pipeline_feed(monkeypatch):
    calls = []
    monkeypatch.setattr(po, 'post_channel', lambda ch, msg: calls.append((ch, msg)) or True)
    po.pipeline_feed('bookend')
    assert calls == [('pipeline-feed', 'bookend')]
