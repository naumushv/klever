#!/usr/bin/env python3
#
# Copyright (c) 2017 ISPRAS (http://www.ispras.ru)
# Institute for System Programming of the Russian Academy of Sciences
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
#

import glob
import os
import subprocess


def execute_cmd(*args, stdin=None, get_output=False):
    print('Execute command "{0}"'.format(' '.join(args)))
    if get_output:
        return subprocess.check_output(args, stdin=stdin).decode('utf8')
    else:
        subprocess.check_call(args, stdin=stdin)


def prepare_environment():
    print('Create user')
    execute_cmd('useradd', 'klever')

    print('Create data directory')
    execute_cmd('mkdir', '/var/lib/klever')

    print('Prepare working directory')
    execute_cmd('mkdir', '/var/lib/klever/work')
    execute_cmd('ln', '-s', '/var/lib/klever/work', 'klever-work')
    execute_cmd('chown', '-LR', 'klever:klever', 'klever-work')

    print('Create soft links for libssl to build new versions of the Linux kernel')
    execute_cmd('ln', '-s', '/usr/include/x86_64-linux-gnu/openssl/opensslconf.h', '/usr/include/openssl/')

    print('Prepare CIF environment')
    execute_cmd('ln', '-s', *glob.glob('/usr/lib/x86_64-linux-gnu/crt*.o'), '/usr/lib')

    print('Enable full local access to all PostgreSQL users to all PostgreSQL databases')
    pg_hba_conf_file = execute_cmd('find', '/etc/postgresql', '-name', 'pg_hba.conf',
                                   get_output=True).rstrip()

    with open(pg_hba_conf_file) as fp:
        pg_hba_conf = fp.readlines()

    with open(pg_hba_conf_file, 'w') as fp:
        for line in pg_hba_conf:
            if line.startswith('local'):
                line = 'local all all trust\n'
            fp.write(line)

    print('Create PostgreSQL database')
    execute_cmd('service', 'postgresql', 'restart')
    execute_cmd('createdb', '-U', 'postgres', '-T', 'template0', '-E', 'utf8', '-O', 'postgres', 'klever')

    print('Prepare media directory')
    execute_cmd('mkdir', '/var/lib/klever/media')
    execute_cmd('chown', '-R', 'www-data:www-data', '/var/lib/klever/media')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--klever-username')
    parser.add_argument('--klever-user-password', default=None)
    parser.add_argument('--klever-working-directory')
    parser.add_argument('--previous-build-configuration-file', default=None)
    parser.add_argument('--new-build-configuration-file')
    args = parser.parse_args()

    prepare_environment(args.klever_configuration_file, args.previous_build_configuration_file,
                     args.new_build_configuration_file, args.non_interactive)