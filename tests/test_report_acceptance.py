from datetime import datetime
import json
from pathlib import Path

import pytest
from scripts import report_acceptance as a
from scripts.report_bundle import BundleError, digest


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setattr(a, 'validate_bundle', lambda *args, **kwargs: {})
    html=tmp_path/'report.html';html.write_text('tested HTML')
    bundle=tmp_path/'report.bundle.json';bundle.write_text('{}')
    image=tmp_path/'image.png';image.write_bytes(b'image fixture')
    render=tmp_path/'report.render.json'
    render.write_text(json.dumps({'status':'passed','htmlSha256':digest(html),'bundleSha256':digest(bundle),
                                 'images':[{'path':str(image),'sha256':digest(image)}],
                                 'viewports':[{'width':1365},{'width':390}]}))
    review=tmp_path/'report.acceptance.json'
    record={'status':'passed','reviewer':'independent-test','reviewedAt':datetime.now().isoformat(),
            'checks':{k:True for k in a.CHECKS},'htmlSha256':digest(html),'bundleSha256':digest(bundle),'renderSha256':digest(render)}
    review.write_text(json.dumps(record))
    return html,bundle,review,record,image,render


def test_hash_bound_independent_review_accepts_limited_edition(report):
    html,bundle,review,record,*_=report
    record['limitations']=['Average entries missing; no comparison or direction inferred']
    review.write_text(json.dumps(record))
    assert a.validate_acceptance(html,bundle,review)['publicationReady']


def test_missing_data_dependent_conclusion_cannot_pass_failed_review(report):
    html,bundle,review,record,*_=report
    record['checks']['facts_match_sources']=False
    record['status']='changes_requested'
    record['issues']=['Conclusion asserts comparison with unavailable average entries']
    review.write_text(json.dumps(record))
    with pytest.raises(BundleError):a.validate_acceptance(html,bundle,review)


@pytest.mark.parametrize('target',['html','bundle','image','render'])
def test_review_cannot_be_reused_for_changed_artifacts(report,target):
    html,bundle,review,record,image,render=report
    {'html':html,'bundle':bundle,'image':image,'render':render}[target].write_text('changed')
    with pytest.raises(BundleError):a.validate_acceptance(html,bundle,review)


def test_missing_browser_images_never_count_as_visual_acceptance(report):
    html,bundle,review,record,image,render=report
    obj=json.loads(render.read_text());obj['images']=[];render.write_text(json.dumps(obj))
    record['renderSha256']=digest(render);review.write_text(json.dumps(record))
    with pytest.raises(BundleError,match='missing'):a.validate_acceptance(html,bundle,review)
