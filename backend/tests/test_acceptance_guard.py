"""A personal/real process must refuse the acceptance entry point before DB access."""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('mode,project,allow',[
    ('real','jlpm-check-0000000000-acceptance-source','yes'),
    ('demo','jlpm-local-real','yes'),
    ('demo','jlpm-check-0000000000-acceptance-source','no'),
])
def test_acceptance_profile_refuses_non_disposable_configuration(mode,project,allow):
    value=subprocess.run([sys.executable,str(Path(__file__).with_name('acceptance_runtime.py')),'api'],
        env={**os.environ,'JL_APP_MODE':mode,'JL_TEST_PROJECT':project,'JL_ALLOW_ACCEPTANCE_CLOCK':allow},capture_output=True,text=True)
    assert value.returncode!=0
    assert 'isolated disposable test image' in value.stderr
