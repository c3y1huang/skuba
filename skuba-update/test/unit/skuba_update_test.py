#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# Copyright (c) 2019 SUSE LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from collections import namedtuple

from pkg_resources import parse_version
import pytest
from mock import patch, call, mock_open, Mock, ANY

from skuba_update.skuba_update import (
    main,
    update,
    run_command,
    run_zypper_command,
    node_name_from_machine_id,
    annotate,
    is_reboot_needed,
    reboot_sentinel_file,
    annotate_updates_available,
    annotate_caasp_release_version,
    get_update_list,
    restart_services,
    REBOOT_REQUIRED_PATH,
    ZYPPER_EXIT_INF_UPDATE_NEEDED,
    ZYPPER_EXIT_INF_RESTART_NEEDED,
    ZYPPER_EXIT_INF_REBOOT_NEEDED,
    KUBE_UPDATES_KEY,
    KUBE_SECURITY_UPDATES_KEY,
    KUBE_DISRUPTIVE_UPDATES_KEY,
    KUBE_CAASP_RELEASE_VERSION_KEY
)
import skuba_update.skuba_update as sup


@patch('subprocess.Popen')
def test_run_command(mock_subprocess):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'stdout', b'stderr')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    result = run_command(['/bin/dummycmd', 'arg1'])
    assert result.output == "stdout"
    assert result.returncode == 0
    assert result.error == 'stderr'

    mock_process.returncode = 1
    result = run_command(['/bin/dummycmd', 'arg1'])
    assert result.output == "stdout"
    assert result.returncode == 1

    mock_process.communicate.return_value = (b'', b'stderr')
    result = run_command(['/bin/dummycmd', 'arg1'])
    assert result.output == ""
    assert result.returncode == 1


@patch('argparse.ArgumentParser.parse_args')
@patch('subprocess.Popen')
def test_main_wrong_version(mock_subprocess, mock_args):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'zypper 1.13.0', b'stderr')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    with pytest.raises(sup.ZypperVersionTooLow) as exc_info:
        main()
    assert "higher is required" in str(
        exc_info.value)


@patch('argparse.ArgumentParser.parse_args')
@patch('subprocess.Popen')
def test_main_bad_format_version(mock_subprocess, mock_args):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'zypper', b'stderr')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    with pytest.raises(sup.UnparseableZypperVersion) as exc_info:
        main()
    assert "Could not parse" in str(
        exc_info.value)


@patch('argparse.ArgumentParser.parse_args')
@patch('subprocess.Popen')
def test_main_no_root(mock_subprocess, mock_args):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'zypper 1.14.15', b'stderr')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    with pytest.raises(sup.PrivilegesTooLow) as exc_info:
        main()
    assert "root privileges" in str(
        exc_info.value)


@patch('argparse.ArgumentParser.parse_args')
@patch('os.geteuid')
@patch('subprocess.Popen')
def test_main_no_kubelet_installed(mock_subprocess, mock_geteuid, mock_args):
    """
    Tests whether zypper search of kubelet fails or
    doesn't return expected ouput
    """

    args = Mock()
    args.annotate_only = False
    mock_args.return_value = args

    mock_geteuid.return_value = 0

    subprocess_returns = [
        (b'<xml', b''),
        (b'refreshed\n', b''),
        (b'zypper 1.14.15', b'')
    ]

    def mock_communicate():
        if len(subprocess_returns) > 1:
            return subprocess_returns.pop()
        else:
            return subprocess_returns[0]

    mock_process = Mock()
    mock_process.communicate.side_effect = mock_communicate
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    with pytest.raises(sup.ZypperSearchException):
        main()


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate_caasp_release_version')
@patch('skuba_update.skuba_update.annotate_updates_available')
@patch('argparse.ArgumentParser.parse_args')
@patch('os.environ.get', new={}.get, spec_set=True)
@patch('os.geteuid')
@patch('subprocess.Popen')
def test_main(
    mock_subprocess, mock_geteuid, mock_args,
    mock_annotate, mock_annotate_version, mock_name
):
    with open('fixtures/uptodate-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet = fd.read()

    return_values = [
        (b'some_service1\nsome_service2', b''),
        (zyp_search_kubelet, b''),
        (b'refresh\n', b''),
        (b'zypper 1.14.15', b'')
    ]

    def mock_communicate():
        if len(return_values) > 1:
            return return_values.pop()
        else:
            return return_values[0]

    args = Mock()
    args.annotate_only = False
    mock_args.return_value = args
    mock_geteuid.return_value = 0
    mock_process = Mock()
    mock_process.communicate.side_effect = mock_communicate
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    main()
    assert mock_subprocess.call_args_list == [
        call(['zypper', '--version'], stdout=-1, stderr=-1, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', 'ref', '-s'],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ['zypper', '--userdata', 'skuba-update', "--non-interactive",
                "--xmlout", "search", "-s", "kubernetes-kubelet"],
            stdout=-1, stderr=-1, env=ANY
        ),
        call([
            'zypper', '--userdata', 'skuba-update', '--non-interactive',
            '--non-interactive-include-reboot-patches', 'patch'
        ], stdout=None, stderr=None, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', 'ps', '-sss'],
            stdout=-1, stderr=-1, env=ANY
        ),
        call(
            ['systemctl', 'restart', 'some_service1'],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ['systemctl', 'restart', 'some_service2'],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ['zypper', '--userdata', 'skuba-update', 'needs-rebooting'],
            stdout=None, stderr=None, env=ANY
        ),
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate_caasp_release_version')
@patch('skuba_update.skuba_update.annotate_updates_available')
@patch('argparse.ArgumentParser.parse_args')
@patch('os.environ.get', new={}.get, spec_set=True)
@patch('os.geteuid')
@patch('subprocess.Popen')
def test_main_unmaintained(
    mock_subprocess, mock_geteuid, mock_args,
    mock_annotate, mock_annotate_version, mock_name
):
    with open('fixtures/outdated-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet = fd.read()

    return_values = [
        (b'some_service1\nsome_service2', b''),
        (zyp_search_kubelet, b''),
        (b'refresh\n', b''),
        (b'zypper 1.14.15', b'')
    ]

    def mock_communicate():
        if len(return_values) > 1:
            return return_values.pop()
        else:
            return return_values[0]

    args = Mock()
    args.annotate_only = False
    mock_args.return_value = args
    mock_geteuid.return_value = 0
    mock_process = Mock()
    mock_process.communicate.side_effect = mock_communicate
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    main()
    assert mock_subprocess.call_args_list == [
        call(['zypper', '--version'], stdout=-1, stderr=-1, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', 'ref', '-s'],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ['zypper', '--userdata', 'skuba-update', "--non-interactive",
                "--xmlout", "search", "-s", "kubernetes-kubelet"],
            stdout=-1, stderr=-1, env=ANY
        ),
        call([
            'zypper', '--userdata', 'skuba-update',
            'modifyrepo', '--disable', 'caasp_40_devel_sle15sp1'
        ], stdout=None, stderr=None, env=ANY),
        call([
            'zypper', '--userdata', 'skuba-update', '--non-interactive',
            '--non-interactive-include-reboot-patches', 'patch'
        ], stdout=None, stderr=None, env=ANY),
        call([
            'zypper', '--userdata', 'skuba-update',
            'modifyrepo', '--enable', 'caasp_40_devel_sle15sp1'
        ], stdout=None, stderr=None, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', 'ps', '-sss'],
            stdout=-1, stderr=-1, env=ANY
        ),
        call(
            ['systemctl', 'restart', 'some_service1'],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ['systemctl', 'restart', 'some_service2'],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ['zypper', '--userdata', 'skuba-update', 'needs-rebooting'],
            stdout=None, stderr=None, env=ANY
        ),
    ]


@patch('subprocess.Popen')
@patch('skuba_update.skuba_update.run_zypper_command')
def test_restart_services_error(mock_zypp_cmd, mock_subprocess, capsys):
    command_type = namedtuple(
        'command', ['output', 'error', 'returncode']
    )

    mock_process = Mock()
    mock_process.communicate.return_value = (b'', b'restart error msg')
    mock_process.returncode = 1
    mock_subprocess.return_value = mock_process

    mock_zypp_cmd.return_value = command_type(
        output="service1\nservice2",
        error='',
        returncode=0
    )

    restart_services()
    out, err = capsys.readouterr()
    assert 'returned non zero exit code' in out


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate_updates_available')
@patch('argparse.ArgumentParser.parse_args')
@patch('os.environ.get', new={}.get, spec_set=True)
@patch('os.geteuid')
@patch('subprocess.Popen')
def test_main_annotate_only(
        mock_subprocess, mock_geteuid, mock_args, mock_annotate, mock_name
):
    args = Mock()
    args.annotate_only = True
    mock_args.return_value = args
    mock_geteuid.return_value = 0
    mock_process = Mock()
    mock_process.communicate.return_value = (b'zypper 1.14.15', b'stderr')
    mock_process.returncode = ZYPPER_EXIT_INF_UPDATE_NEEDED
    mock_subprocess.return_value = mock_process
    main()
    assert mock_subprocess.call_args_list == [
        call(['zypper', '--version'], stdout=-1, stderr=-1, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', 'ref', '-s'],
            stdout=None, stderr=None, env=ANY
        ),
        call([
            'rpm', '-q', 'caasp-release', '--queryformat', '%{VERSION}'
        ], stdout=-1, stderr=-1, env=ANY),
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate_updates_available')
@patch('argparse.ArgumentParser.parse_args')
@patch('os.environ.get', new={}.get, spec_set=True)
@patch('os.geteuid')
@patch('subprocess.Popen')
def test_main_zypper_returns_100(
        mock_subprocess, mock_geteuid, mock_args, mock_annotate, mock_name
):
    with open('fixtures/uptodate-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet = fd.read()

    return_values = [
        (b'', b''),
        (zyp_search_kubelet, b''),
        (b'refresh\n', b''),
        (b'zypper 1.14.15', b'')
    ]

    def mock_communicate():
        if len(return_values) > 1:
            return return_values.pop()
        else:
            return return_values[0]

    args = Mock()
    args.annotate_only = False
    mock_args.return_value = args
    mock_geteuid.return_value = 0
    mock_process = Mock()
    mock_process.communicate.side_effect = mock_communicate
    mock_process.returncode = ZYPPER_EXIT_INF_RESTART_NEEDED
    mock_subprocess.return_value = mock_process
    main()
    assert mock_subprocess.call_args_list == [
        call(['zypper', '--version'], stdout=-1, stderr=-1, env=ANY),
        call([
            'zypper', '--userdata', 'skuba-update', 'ref', '-s'
        ], stdout=None, stderr=None, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', "--non-interactive",
                "--xmlout", "search", "-s", "kubernetes-kubelet"],
            stdout=-1, stderr=-1, env=ANY
        ),
        call([
            'zypper', '--userdata', 'skuba-update', '--non-interactive',
            '--non-interactive-include-reboot-patches', 'patch'
        ], stdout=None, stderr=None, env=ANY),
        call([
            'zypper', '--userdata', 'skuba-update', '--non-interactive',
            '--non-interactive-include-reboot-patches', 'patch'
        ], stdout=None, stderr=None, env=ANY),
        call(
            ['zypper', '--userdata', 'skuba-update', 'ps', '-sss'],
            stdout=-1, stderr=-1, env=ANY
        ),
        call([
            'rpm', '-q', 'caasp-release', '--queryformat', '%{VERSION}'
        ], stdout=-1, stderr=-1, env=ANY),
        call([
            'zypper', '--userdata', 'skuba-update', 'needs-rebooting'
        ], stdout=None, stderr=None, env=ANY),
    ]


@patch('pathlib.Path.is_file')
@patch('subprocess.Popen')
def test_update_zypper_is_fine_but_created_reboot_required(
        mock_subprocess, mock_is_file
):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'stdout', b'stderr')

    mock_process.returncode = ZYPPER_EXIT_INF_REBOOT_NEEDED
    mock_subprocess.return_value = mock_process
    mock_is_file.return_value = True

    exception = False
    try:
        reboot_sentinel_file(update())
    except PermissionError as e:
        exception = True
        msg = 'Permission denied: \'{0}\''.format(REBOOT_REQUIRED_PATH)
        assert msg in str(e)
    assert exception


@patch('subprocess.Popen')
def test_run_zypper_command(mock_subprocess):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'stdout', b'stderr')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    assert run_zypper_command(['patch']) == 0
    mock_process.returncode = ZYPPER_EXIT_INF_RESTART_NEEDED
    mock_subprocess.return_value = mock_process
    assert run_zypper_command(
        ['patch']) == ZYPPER_EXIT_INF_RESTART_NEEDED


@patch('subprocess.Popen')
def test_run_zypper_command_failure(mock_subprocess):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'', b'')
    mock_process.returncode = 1
    mock_subprocess.return_value = mock_process
    exception = False
    try:
        run_zypper_command(['patch']) == 'stdout'
    except Exception as e:
        exception = True
        assert '"zypper --userdata skuba-update patch" failed' in str(e)
    assert exception


@patch('builtins.open',
       mock_open(read_data='9ea12911449eb7b5f8f228294bf9209a'))
@patch('subprocess.Popen')
@patch('json.loads')
def test_node_name_from_machine_id(mock_loads, mock_subprocess):
    json_node_object = {
        'items': [
            {
                'metadata': {
                    'name': 'my-node-1'
                },
                'status': {
                    'nodeInfo': {
                        'machineID': '49f8e2911a1449b7b5ef2bf92282909a'
                    }
                }
            },
            {
                'metadata': {
                    'name': 'my-node-2'
                },
                'status': {
                    'nodeInfo': {
                        'machineID': '9ea12911449eb7b5f8f228294bf9209a'
                    }
                }
            }
        ]
    }
    breaking_json_node_object = {'Items': []}

    mock_process = Mock()
    mock_process.communicate.return_value = (json.dumps(json_node_object)
                                             .encode(), b'')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    mock_loads.return_value = json_node_object
    assert node_name_from_machine_id() == 'my-node-2'

    json_node_object2 = json_node_object
    json_node_object2['items'][1]['status']['nodeInfo']['machineID'] = \
        'another-id-that-doesnt-reflect-a-node'
    mock_loads.return_value = json_node_object2
    exception = False
    try:
        node_name_from_machine_id() == 'my-node-2'
    except Exception as e:
        exception = True
        assert 'Node name could not be determined' in str(e)
    assert exception

    mock_loads.return_value = breaking_json_node_object
    exception = False
    try:
        node_name_from_machine_id() == 'my-node-2'
    except Exception as e:
        exception = True
        assert 'Unexpected format' in str(e)
    assert exception
    exception = False
    mock_process.returncode = 1
    try:
        node_name_from_machine_id() == 'my-node'
    except Exception as e:
        exception = True
        assert 'Kubectl failed getting nodes list' in str(e)
    assert exception


@patch('subprocess.Popen')
def test_annotate(mock_subprocess, capsys):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'node/my-node-1 annotated',
                                             b'stderr')
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    assert annotate(
        'node', 'my-node-1',
        KUBE_DISRUPTIVE_UPDATES_KEY, 'yes'
    ) == 'node/my-node-1 annotated'
    mock_process.returncode = 1
    annotate(
        'node', 'my-node-1',
        KUBE_DISRUPTIVE_UPDATES_KEY, 'yes'
    )
    out, err = capsys.readouterr()
    assert 'Warning! kubectl returned non zero exit code' in out


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate')
@patch('subprocess.Popen')
def test_annotate_updates_empty(mock_subprocess, mock_annotate, mock_name):
    mock_name.return_value = 'mynode'
    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'<stream><update-status><update-list>'
        b'</update-list></update-status></stream>', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    annotate_updates_available(mock_name.return_value)
    assert mock_subprocess.call_args_list == [
        call(
            ['zypper', '--userdata', 'skuba-update',
             '--non-interactive', '--xmlout', 'list-patches'],
            stdout=-1, stderr=-1, env=ANY
        )
    ]
    assert mock_annotate.call_args_list == [
        call('node', 'mynode', KUBE_UPDATES_KEY, 'no'),
        call('node', 'mynode', KUBE_SECURITY_UPDATES_KEY, 'no'),
        call('node', 'mynode', KUBE_DISRUPTIVE_UPDATES_KEY, 'no')
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate')
@patch('subprocess.Popen')
def test_annotate_updates(mock_subprocess, mock_annotate, mock_name):
    mock_name.return_value = 'mynode'
    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'<stream><update-status><update-list><update interactive="message">'
        b'</update></update-list></update-status></stream>', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process
    annotate_updates_available(mock_name.return_value)
    assert mock_subprocess.call_args_list == [
        call(
            ['zypper', '--userdata', 'skuba-update',
             '--non-interactive', '--xmlout', 'list-patches'],
            stdout=-1, stderr=-1, env=ANY
        )
    ]
    assert mock_annotate.call_args_list == [
        call('node', 'mynode', KUBE_UPDATES_KEY, 'yes'),
        call('node', 'mynode', KUBE_SECURITY_UPDATES_KEY, 'no'),
        call('node', 'mynode', KUBE_DISRUPTIVE_UPDATES_KEY, 'yes')
    ]


@patch("skuba_update.skuba_update.node_name_from_machine_id")
@patch("builtins.open", read_data="aa59dc0c5fe84247a77c26780dd0b3fd")
@patch('subprocess.Popen')
def test_annotate_updates_available(mock_subprocess, mock_open, mock_name):
    mock_name.return_value = 'mynode'

    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'<stream><update-status><update-list><update interactive="message">'
        b'</update></update-list></update-status></stream>', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    annotate_updates_available(mock_name.return_value)

    assert mock_subprocess.call_args_list == [
        call(
            ['zypper', '--userdata', 'skuba-update',
             '--non-interactive', '--xmlout', 'list-patches'],
            stdout=-1, stderr=-1, env=ANY
        ),
        call(
            ["kubectl", "annotate", "--overwrite", "node",
             "mynode", "caasp.suse.com/has-updates=yes"],
            stdout=-1, stderr=-1, env=ANY
        ),
        call(
            ["kubectl", "annotate", "--overwrite", "node",
             "mynode", "caasp.suse.com/has-security-updates=no"],
            stdout=-1, stderr=-1, env=ANY
        ),
        call(
            ["kubectl", "annotate", "--overwrite", "node",
             "mynode", "caasp.suse.com/has-disruptive-updates=yes"],
            stdout=-1, stderr=-1, env=ANY
        )
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate')
@patch('subprocess.Popen')
def test_annotate_updates_bad_xml(mock_subprocess, mock_annotate, mock_name):
    mock_name.return_value = 'mynode'
    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'<update-status><update-list><update interactive="message">'
        b'</update></update-list></update-status>', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    annotate_updates_available(mock_name.return_value)
    assert mock_subprocess.call_args_list == [
        call(
            ['zypper', '--userdata', 'skuba-update',
             '--non-interactive', '--xmlout', 'list-patches'],
            stdout=-1, stderr=-1, env=ANY
        )
    ]
    assert mock_annotate.call_args_list == [
        call('node', 'mynode', KUBE_UPDATES_KEY, 'no'),
        call('node', 'mynode', KUBE_SECURITY_UPDATES_KEY, 'no'),
        call('node', 'mynode', KUBE_DISRUPTIVE_UPDATES_KEY, 'no')
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate')
@patch('subprocess.Popen')
def test_annotate_updates_security(
        mock_subprocess, mock_annotate, mock_name
):
    mock_name.return_value = 'mynode'
    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'<stream><update-status><update-list>'
        b'<update interactive="false" category="security">'
        b'</update></update-list></update-status></stream>', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    annotate_updates_available(mock_name.return_value)
    assert mock_subprocess.call_args_list == [
        call(
            ['zypper', '--userdata', 'skuba-update',
             '--non-interactive', '--xmlout', 'list-patches'],
            stdout=-1, stderr=-1, env=ANY
        )
    ]
    assert mock_annotate.call_args_list == [
        call('node', 'mynode', KUBE_UPDATES_KEY, 'yes'),
        call('node', 'mynode', KUBE_SECURITY_UPDATES_KEY, 'yes'),
        call('node', 'mynode', KUBE_DISRUPTIVE_UPDATES_KEY, 'no')
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate')
@patch('subprocess.Popen')
def test_annotate_updates_available_is_reboot(
        mock_subprocess, mock_annotate, mock_name
):
    mock_name.return_value = 'mynode'

    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'<stream><update-status><update-list><update interactive="reboot">'
        b'</update></update-list></update-status></stream>', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    annotate_updates_available(mock_name.return_value)
    assert mock_subprocess.call_args_list == [
        call(
            ['zypper', '--userdata', 'skuba-update',
             '--non-interactive', '--xmlout', 'list-patches'],
            stdout=-1, stderr=-1, env=ANY
        )
    ]
    assert mock_annotate.call_args_list == [
        call('node', 'mynode', KUBE_UPDATES_KEY, 'yes'),
        call('node', 'mynode', KUBE_SECURITY_UPDATES_KEY, 'no'),
        call('node', 'mynode', KUBE_DISRUPTIVE_UPDATES_KEY, 'yes')
    ]


@patch('skuba_update.skuba_update.node_name_from_machine_id')
@patch('skuba_update.skuba_update.annotate')
@patch('subprocess.Popen')
def test_annotate_caasp_release_version(
    mock_subprocess, mock_annotate, mock_name
):
    mock_name.return_value = 'mynode'

    mock_process = Mock()
    mock_process.communicate.return_value = (
        b'1.2.3', b''
    )
    mock_process.returncode = 0
    mock_subprocess.return_value = mock_process

    annotate_caasp_release_version(mock_name.return_value)
    assert mock_subprocess.call_args_list == [
        call(
            ['rpm', '-q', 'caasp-release', '--queryformat', '%{VERSION}'],
            stdout=-1, stderr=-1, env=ANY
        )
    ]
    assert mock_annotate.call_args_list == [
        call('node', 'mynode', KUBE_CAASP_RELEASE_VERSION_KEY, '1.2.3'),
    ]


@patch('subprocess.Popen')
def test_is_reboot_needed_truthy(mock_subprocess):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'', b'')
    mock_process.returncode = ZYPPER_EXIT_INF_REBOOT_NEEDED
    mock_subprocess.return_value = mock_process

    assert is_reboot_needed()


@patch('subprocess.Popen')
def test_is_reboot_needed_falsey(mock_subprocess):
    mock_process = Mock()
    mock_process.communicate.return_value = (b'', b'')
    mock_process.returncode = ZYPPER_EXIT_INF_RESTART_NEEDED
    mock_subprocess.return_value = mock_process

    assert not is_reboot_needed()


def test_get_update_list_bad_xml():
    assert get_update_list('<xml') is None


@patch('subprocess.Popen')
def test_kubeletpkgdetails_badxml(mock_subprocess):

    mock_process = Mock()
    attrs = {'communicate.return_value': (b"<xml", b""), 'returncode': 0}
    mock_process.configure_mock(**attrs)
    mock_subprocess.return_value = mock_process

    assert sup.get_kubelet_packages_details() is None


@patch('subprocess.Popen')
def test_kubeletpkgdetails_nopkgmatch(mock_subprocess):
    no_pkgmatch_xml = (
        b"<?xml version='1.0'?>"
        b"<stream>"
        b'<message type="info">Loading repository data...</message>'
        b'<message type="info">Reading installed packages...</message>'
        b'<message type="info">No matching items found.</message>'
        b"</stream>"
    )

    mock_process = Mock()
    attrs = {'communicate.return_value': (
        no_pkgmatch_xml, b""), 'returncode': 0}
    mock_process.configure_mock(**attrs)
    mock_subprocess.return_value = mock_process

    assert sup.get_kubelet_packages_details() is None


def test_parsekubeletpkglist_nokubeletpkg():
    with pytest.raises(sup.ZypperSearchException) as excinfo:
        sup.parse_kubelet_pkglist(None)
    assert "Unparseable package list" in str(excinfo.value)


def test_parsekubeletpkglist_kubelet_notinstalled():
    with open('fixtures/notinstalled-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet_xmldata = fd.read()

    with pytest.raises(sup.ZypperSearchException) as excinfo:
        sup.parse_kubelet_pkglist(
            sup.parse_zyppersearch_xml(
                zyp_search_kubelet_xmldata
            )
        )
    assert "not installed" in str(excinfo.value)


def test_parsekubeletpkglist_uptodate_kubelet():
    """
    Tests if the kubelet installed is up to date
    returns the right structure
    """
    with open('fixtures/uptodate-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet_xmldata = fd.read()

    expected = dict()
    expected['repos_containing_upgrades'] = set()
    expected['latest'] = parse_version('1.18')
    expected['installed'] = parse_version('1.18')

    assert expected == sup.parse_kubelet_pkglist(
        sup.parse_zyppersearch_xml(
            zyp_search_kubelet_xmldata
        )
    )


def test_parsekubeletpkglist_outdated_kubelet():
    """
    Tests if the kubelet installed is not the latest
    returns the right structure
    """
    with open('fixtures/outdated-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet_xmldata = fd.read()

    expected = dict()
    expected['repos_containing_upgrades'] = set()
    expected['latest'] = parse_version('1.18')
    expected['installed'] = parse_version('1.16')
    expected['repos_containing_upgrades'].add("caasp_40_devel_sle15sp1")

    assert expected == sup.parse_kubelet_pkglist(
        sup.parse_zyppersearch_xml(
            zyp_search_kubelet_xmldata
        )
    )


def test_parsekubeletpkglist_justonepkg():
    """
    Test if everything works fine with a unique package
    """
    with open('fixtures/unique-kubelet.xml', 'rb') as fd:
        zyp_search_kubelet_xmldata = fd.read()

    expected = dict()
    expected['repos_containing_upgrades'] = set()
    expected['latest'] = parse_version('1.16')
    expected['installed'] = parse_version('1.16')

    assert expected == sup.parse_kubelet_pkglist(
        sup.parse_zyppersearch_xml(
            zyp_search_kubelet_xmldata
        )
    )
    return False


def test_is_supported():
    a = parse_version('1.1')
    b = parse_version('1.2')
    c = parse_version('1.1')
    assert not sup.is_supported(a, b)
    assert sup.is_supported(a, c)


@patch('subprocess.Popen')
def test_modify_channel_wronginput(mock_subprocesspopen):
    mock_process = Mock()
    attrs = {'communicate.return_value': ("", ""), 'returncode': 1}
    mock_process.configure_mock(**attrs)
    mock_subprocesspopen.return_value = mock_process

    with pytest.raises(Exception) as excinfo:
        sup.modify_repos(["a", "b"], commandoption="beepbap")
    assert "Failed to modify one or more repos" in str(excinfo.value)


@patch('subprocess.Popen')
def test_enable_pkg_channel(mock_subprocesspopen):
    mock_process = Mock()
    attrs = {'communicate.return_value': ("", ""), 'returncode': 0}
    mock_process.configure_mock(**attrs)
    mock_subprocesspopen.return_value = mock_process

    repos = ["a", "b"]

    sup.modify_repos(repos, commandoption="--enable")

    assert mock_subprocesspopen.call_args_list == [
        call(
            ["zypper",  "--userdata", "skuba-update",
                "modifyrepo", "--enable", "a"],
            stdout=None, stderr=None, env=ANY
        ),
        call(
            ["zypper",  "--userdata", "skuba-update",
                "modifyrepo", "--enable", "b"],
            stdout=None, stderr=None, env=ANY
        )
    ]


@patch('subprocess.Popen')
def test_disable_pkg_channel(mock_subprocesspopen):
    mock_process = Mock()
    attrs = {'communicate.return_value': ("", ""), 'returncode': 0}
    mock_process.configure_mock(**attrs)
    mock_subprocesspopen.return_value = mock_process

    repos = ["a"]

    sup.modify_repos(repos, commandoption="--disable")

    assert mock_subprocesspopen.call_args_list == [
        call(
            ["zypper",  "--userdata", "skuba-update",
                "modifyrepo", "--disable", "a"],
            stdout=None, stderr=None, env=ANY
        )
    ]
