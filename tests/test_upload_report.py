from scripts import upload_report


def test_fixed_paths_and_reader_url():
    assert upload_report.object_path('daily') == 'daily/latest.html'
    assert upload_report.storage_url('https://proj.supabase.co', 'weekly/latest.html').endswith('/bias-reports/weekly/latest.html')
    assert upload_report.reader_url('weekly') == 'https://www.laa-inc.com/reports/weekly'


def test_missing_report_is_soft_for_generation_only(tmp_path, capsys):
    assert upload_report.upload(tmp_path/'missing.html', 'daily') is None
    assert 'WARN' in capsys.readouterr().out


def test_unreviewed_html_cannot_upload(tmp_path, monkeypatch):
    p = tmp_path/'report.html'; p.write_text('<html>report</html>')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden')))
    assert upload_report.upload(p, 'weekly', publish=True) is None
    assert upload_report.main([str(p), '--mode', 'weekly', '--publish']) == 1
