"""Presenter paths must expose real calculations, blocks and provenance."""
import json
import pymupdf
import pytest
from fastapi.testclient import TestClient
from trustsight.api import main

@pytest.fixture
def client():
    # Every write path now records an actor (spec v2 s2). These tests
    # exercise engineering behaviour, so they act as the role that is
    # allowed to; the role rules themselves are tested in test_roles.py.
    return TestClient(main.app, headers={'x-trustsight-user': 'priya.raman@demo-client.com'})

def start(client, scenario="clarification"):
    response = client.post('/runs', json={'project_id': 'atlantic-demo', 'scenario': scenario})
    assert response.status_code == 200
    return response.json()['run_id']

def snapshot(client, run_id):
    response = client.get(f'/runs/{run_id}/workspace')
    assert response.status_code == 200
    return response.json()

def approve(client, run_id, field, value, role):
    return client.post(f'/runs/{run_id}/clarifications', json={
        'field_name': field, 'value': value, 'role': role, 'mark': 'P1',
        'approver': 'Demo tester (simulated)', 'rationale': 'Sample walkthrough assumption',
    })

def test_partial_then_complete_release_and_stable_run_id(client):
    run_id = start(client)
    initial = snapshot(client, run_id)
    assert initial['released_mass_kg'] == 0
    assert len(initial['questions']) == 2
    assert approve(client, run_id, 'legs', {'A':510, 'B':11955}, 'longitudinal').status_code == 200
    partial = snapshot(client, run_id)
    assert partial['run']['run_id'] == run_id
    assert [i['quantity'] for i in partial['schedule']['items']] == [72]
    assert len(partial['questions']) == 1
    assert approve(client, run_id, 'run_length_mm', 12250, 'spiral').status_code == 200
    done = snapshot(client, run_id)
    assert [i['quantity'] for i in done['schedule']['items']] == [72,216]
    assert done['released_mass_kg'] == 5991.4
    assert done['questions'] == []
    assert all(r['match'] for r in done['benchmark']['rows'])
    assert done['evidence']['chain_valid']
    assert len(done['history']) == 2
    assert {r['run_id'] for r in done['evidence']['records']} == {run_id}
    assert len([s for s in done['steps'] if s['status']=='executed']) < 23
    assert not done['live_model']
    assert 'O (' not in done['schedule']['items'][1]['explanation']['length_basis']
    assert 'SAMPLE - not for construction' in client.get(f'/runs/{run_id}/export/bbs').text
    exported = json.loads(client.get(f'/runs/{run_id}/export/evidence').text)
    assert exported['evidence']['chain_valid']

def test_conflict_stays_blocked_after_answering_other_questions(client):
    run_id = start(client, 'conflict')
    approve(client, run_id, 'legs', {'A':510, 'B':11955}, 'longitudinal')
    approve(client, run_id, 'run_length_mm', 12250, 'spiral')
    data = snapshot(client, run_id)
    assert data['schedule']['items'] == []
    assert data['released_mass_kg'] == 0
    assert '6 piles' in data['elements'][0]['conflicts'][0]
    assert data['schedule']['exceptions']

@pytest.mark.parametrize('field,value', [('legs', {}), ('legs', {'A':-1}), ('run_length_mm',0), ('run_length_mm','12250')])
def test_invalid_clarification_does_not_add_evidence(client, field, value):
    run_id = start(client)
    before = snapshot(client, run_id)['evidence']['records']
    assert approve(client,run_id,field,value,'longitudinal').status_code == 422
    assert snapshot(client,run_id)['evidence']['records'] == before

def test_pdf_intake_and_source_render(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'CORPUS', tmp_path)
    with pymupdf.open() as doc:
        page=doc.new_page()
        page.insert_text((50,50),'TEST SOURCE DRAWING - unsupported element, no pile schedule')
        payload=doc.tobytes()
    upload=client.post('/projects/upload',content=payload)
    assert upload.status_code == 200
    project_id=upload.json()['project_id']
    r=client.post('/runs',json={'project_id':project_id}).json()['run_id']
    data=snapshot(client,r)
    assert data['seeded'] is False
    assert data['rulebook']['approved_by'] is None
    assert data['schedule']['items'] == []
    assert client.get(f'/runs/{r}/document').json()['pages']==1
    image=client.get(f'/runs/{r}/drawing')
    assert image.status_code==200 and image.content.startswith(b'\x89PNG')
    assert client.get(f'/runs/{r}/drawing?page=2').status_code==404
    assert client.post('/projects/upload',content=b'not a pdf').status_code==422


def test_dashboard_assets_and_path_validation(client):
    assert client.get('/').status_code == 200
    assert client.get('/assets/workspace.js').status_code == 200
    # the stock photographs were replaced by inline SVG diagrams; what the
    # image must still carry is the vendored viewer dependency, because a
    # client network that blocks public CDNs would otherwise show empty 3D
    assert client.get('/assets/logo.svg').status_code == 200
    assert client.get('/assets/vendor/three.module.js').status_code == 200
    assert client.post('/runs',json={'project_id':'../elsewhere'}).status_code==400
