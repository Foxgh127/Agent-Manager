import pytest
from scripts.release_notes import select_release_notes


def test_release_body_excludes_other_versions_and_retains_its_subsections():
    text = '# Agent Manager 1.1.2\n\n- Fixed radar.\n\n## Details\nActual changes.\n\n## 1.0.0\nOld notes.\n'
    selected = select_release_notes(text, '1.1.2')
    assert 'Details' in selected and 'Actual changes.' in selected
    assert '1.0.0' not in selected and 'Old notes' not in selected
    assert select_release_notes(text, '1.0.0') == '## 1.0.0\nOld notes.\n'


def test_wrong_or_repeated_release_version_is_rejected():
    with pytest.raises(ValueError):
        select_release_notes('# Agent Manager 1.1.0\n- Old\n', '1.1.2')
    with pytest.raises(ValueError):
        select_release_notes('# 1.1.2\nA\n## 1.1.2\nB\n', '1.1.2')
