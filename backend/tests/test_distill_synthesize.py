import br8n.knowledge_graph.distill as d
from br8n.config import get_config


async def test_synthesize_returns_candidates(monkeypatch):
    async def fake_completion(**kw):
        return d.ConceptBatch(concepts=[
            d.ConceptCandidate(claim="X compounds Y", body="because Z", evidence=["f1", "f2"]),
        ])
    monkeypatch.setattr(d, "structured_completion", fake_completion)

    findings = [{"id": "f1", "title": "X", "content": "x details"},
                {"id": "f2", "title": "Y", "content": "y details"}]
    out = await d.synthesize(findings, get_config().concept)
    assert len(out) == 1
    assert out[0].claim == "X compounds Y"
    assert out[0].evidence == ["f1", "f2"]


async def test_synthesize_empty_findings_no_llm(monkeypatch):
    called = False
    async def fake_completion(**kw):
        nonlocal called
        called = True
        return d.ConceptBatch(concepts=[])
    monkeypatch.setattr(d, "structured_completion", fake_completion)
    out = await d.synthesize([], get_config().concept)
    assert out == [] and called is False
