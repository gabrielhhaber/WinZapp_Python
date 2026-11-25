"""Dependency notices describe the resolved packages, without machine paths."""
from build_neteq import dependency_notices


def test_resolved_dependency_notices_keep_versions_and_license_text(tmp_path):
    (tmp_path/'Cargo.toml').write_text('')
    (tmp_path/'LICENSE-MIT').write_text('Original copyright and permission')
    metadata={'resolve':{'nodes':[{'id':'used'}]},'packages':[
        {'id':'used','name':'dependency','version':'1.2.3','source':'registry',
         'license':'MIT','manifest_path':str(tmp_path/'Cargo.toml')},
        {'id':'unused','name':'unused','version':'9','source':'registry','manifest_path':'missing'},
    ]}
    result=dependency_notices(metadata)
    assert 'dependency 1.2.3 (MIT)' in result
    assert 'Original copyright and permission' in result
    assert 'unused' not in result and str(tmp_path) not in result
