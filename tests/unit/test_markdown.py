"""The docs renderer: the subset FEATURES.md and API.md use, escaped, with unsafe links dropped."""

from __future__ import annotations

from pathlib import Path

from radreport.api.markdown import render, slug


def test_blocks_and_inline() -> None:
    out = render(
        """# Title

Some **bold**, *italic* and `code <x>` with a [link](API.md#appendix) and [ext](https://example.com).

| A | B |
|---|---:|
| `x` | **2** |

- one
  continued
  - nested
- two
1. first

- [x] done item
- [ ] open item

> quoted *text*

```python
print("<hi>")
```
---
<!-- secret-note -->
""",
        link_base="/docs-base/",
    )
    html = out.html
    assert '<h1 id="title">' in html and out.toc == [(1, "title", "Title")]
    assert "<strong>bold</strong>" in html and "<em>italic</em>" in html and "<code>code &lt;x&gt;</code>" in html
    assert 'href="/docs-base/API.md#appendix"' in html and 'rel="noopener" target="_blank"' in html
    assert "<th>A</th>" in html and "<td><strong>2</strong></td>" in html
    assert "<ul><li>one continued<ul><li>nested</li></ul></li><li>two</li></ul><ol><li>first</li></ol>" in html
    assert 'class="check done"' in html and 'class="check"' in html
    assert "<blockquote><p>quoted <em>text</em></p></blockquote>" in html
    assert '<pre><code class="language-python">print(&quot;&lt;hi&gt;&quot;)</code></pre>' in html
    assert "<hr>" in html and "secret-note" not in html


def test_nothing_unsafe_survives() -> None:
    html = render("<script>alert(1)</script> [x](javascript:alert(1)) [y](data:text/html,hi) <img src=x onerror=alert(1)>").html
    assert "<script>" not in html and "javascript:" not in html and "data:" not in html and "<img" not in html


def test_slugs_match_github_and_the_project_docs_render() -> None:
    assert slug("Phase 0 — Is the service up?") == "phase-0--is-the-service-up"
    assert slug("3.2 From upload to draft") == "32-from-upload-to-draft"
    for doc in ("FEATURES.md", "API.md"):
        rendered = render(Path(doc).read_text())
        assert len(rendered.toc) > 5 and "<table>" in rendered.html
