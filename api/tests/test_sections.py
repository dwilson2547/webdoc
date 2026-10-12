import pytest

from webdoc import sections
from webdoc.sections import SectionError

DOC = """# Title

Intro.

## Architecture

Overview.

### Auth

Uses authentik.

### Storage

Postgres.

```bash
# not a heading
```

## Notes

Last.
"""


def test_outline_ignores_fenced_code():
    paths = [tuple(h["path"]) for h in sections.outline(DOC)]
    assert paths == [("Title",), ("Title", "Architecture"), ("Title", "Architecture", "Auth"),
                     ("Title", "Architecture", "Storage"), ("Title", "Notes")]


def test_get_section_includes_subsections():
    text = sections.get(DOC, ["Architecture"])
    assert text.startswith("## Architecture") and "### Storage" in text and "## Notes" not in text


def test_path_may_skip_levels_and_is_case_insensitive():
    assert sections.find(DOC, ["title", "auth"]).text == "Auth"
    assert sections.find(DOC, "storage").level == 3


def test_unknown_and_ambiguous_paths_explain_themselves():
    with pytest.raises(SectionError, match="Sections: Title"):
        sections.find(DOC, ["Nope"])
    doc = "## A\n\n### X\n\n## B\n\n### X\n"
    with pytest.raises(SectionError, match="matches 2 sections"):
        sections.find(doc, ["X"])
    assert sections.find(doc, ["B", "X"]).line == 6


def test_replace_keeps_heading_and_rest_of_document():
    out = sections.replace(DOC, ["Auth"], "### Auth\n\nUses OIDC now.\n")
    assert "Uses OIDC now." in out and "authentik" not in out
    assert out.count("### Auth") == 1
    assert out.split("### Auth")[0] == DOC.split("### Auth")[0]
    assert out.endswith("## Notes\n\nLast.\n")


def test_append_goes_after_subsections():
    out = sections.append(DOC, ["Architecture"], "Trailing.")
    assert out.index("Trailing.") > out.index("# not a heading")
    assert out.index("Trailing.") < out.index("## Notes")


def test_insert_after_parent_and_at_end():
    out = sections.insert(DOC, "Search", "pgvector", after=["Auth"])
    assert "### Search\n\npgvector\n\n### Storage" in out
    out = sections.insert(DOC, "Ops", parent=["Architecture"])
    assert out.index("### Ops") < out.index("## Notes")
    out = sections.insert(DOC, "Appendix", "x")
    assert out.endswith("## Appendix\n\nx\n")


def test_delete_section():
    out = sections.delete(DOC, ["Architecture"])
    assert "Auth" not in out and "## Notes" in out and "Intro.\n\n## Notes" in out


def test_replace_text_must_be_unique():
    assert "Postgres 16." in sections.replace_text(DOC, "Postgres.", "Postgres 16.")
    with pytest.raises(SectionError, match="occurs 0 times"):
        sections.replace_text(DOC, "missing", "x")


def test_apply_reports_each_step_and_names_the_failing_op():
    body, done = sections.apply(DOC, [
        {"op": "replace_section", "heading_path": ["Notes"], "content": "New."},
        {"op": "append", "content": "PS"}])
    assert done == ["replaced §Notes", "appended"] and body.endswith("New.\n\nPS\n")
    with pytest.raises(SectionError, match=r"op 2 \(delete_section\)"):
        sections.apply(DOC, [{"op": "append", "content": "x"},
                             {"op": "delete_section", "heading_path": ["Nope"]}])
