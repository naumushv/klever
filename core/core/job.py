import copy
import hashlib
import importlib
import json
import multiprocessing
import os
import re
import sys
import tarfile
import traceback

import core.utils


class Job(core.utils.CallbacksCaller):
    FORMAT = 1
    ARCHIVE = 'job.tar.gz'
    DIR = 'job'
    CLASS_FILE = os.path.join(DIR, 'class')
    DEFAULT_CONF_FILE = 'core.json'
    JOB_CLASS_COMPONENTS = {
        'Verification of Linux kernel modules': [
            'LKBCE',
            'LKVOG',
            'AVTG',
            'VTG',
        ],
    }

    def __init__(self, logger, id, type=None):
        self.logger = logger
        self.id = id
        self.logger.debug('Job identifier is "{0}"'.format(id))
        self.parent = {}
        self.name_prefix = None
        self.name = None
        self.work_dir = None
        self.mqs = {}
        self.locks = {}
        self.uploading_reports_process = None
        self.data = None
        self.data_lock = None
        self.type = type
        self.components_common_conf = None
        self.sub_jobs = []
        self.components = []
        self.callbacks = {}
        self.component_processes = []

    def decide(self, conf, mqs, locks, uploading_reports_process):
        self.logger.info('Decide job')

        self.mqs = mqs
        self.locks = locks
        self.uploading_reports_process = uploading_reports_process
        self.data = multiprocessing.Manager().dict()
        self.data_lock = multiprocessing.Lock()
        self.extract_archive()
        self.get_class()
        self.get_common_components_conf(conf)
        self.get_sub_jobs()

        if self.sub_jobs:
            self.logger.info('Decide sub-jobs')

            sub_job_solvers_num = core.utils.get_parallel_threads_num(self.logger, self.components_common_conf,
                                                                      'Sub-jobs processing')
            self.logger.debug('Sub-jobs will be decided in parallel by "{0}" solvers'.format(sub_job_solvers_num))

            self.mqs['sub-job indexes'] = multiprocessing.Queue()
            for i in range(len(self.sub_jobs)):
                self.mqs['sub-job indexes'].put(i)
            for i in range(sub_job_solvers_num):
                self.mqs['sub-job indexes'].put(None)

            sub_job_solver_processes = []
            try:
                for i in range(sub_job_solvers_num):
                    p = multiprocessing.Process(target=self.decide_sub_job, name='Worker ' + str(i))
                    p.start()
                    sub_job_solver_processes.append(p)

                self.logger.info('Wait for sub-jobs')
                while True:
                    operating_sub_job_solvers_num = 0

                    for p in sub_job_solver_processes:
                        p.join(1.0 / len(sub_job_solver_processes))
                        if p.exitcode:
                            exit(1)
                        operating_sub_job_solvers_num += p.is_alive()

                    if not operating_sub_job_solvers_num:
                        break
            finally:
                for p in sub_job_solver_processes:
                    if p.is_alive():
                        p.terminate()
        else:
            # Klever Core working directory is used for the only sub-job that is job itself.
            self.__decide_sub_job()

    def decide_sub_job(self):
        while True:
            sub_job_index = self.mqs['sub-job indexes'].get()

            if sub_job_index is None:
                self.logger.debug('Sub-job indexes message queue was terminated')
                break

            try:
                self.sub_jobs[sub_job_index].__decide_sub_job()
            except SystemExit:
                if not self.components_common_conf['ignore failed sub-jobs']:
                    sys.exit(1)

    def __decide_sub_job(self):
        if self.name:
            self.logger.info('Decide sub-job of type "{0}" with identifier "{1}"'.format(self.type, self.id))

        # All sub-job names should be unique, so there shouldn't be any problem to create directories with these names
        # to be used as working directories for corresponding sub-jobs. Jobs without sub-jobs don't have names.
        if self.name:
            os.makedirs(self.work_dir)

        # Do not produce any reports until changing directory. Otherwise there can be races between various sub-jobs.
        with core.utils.Cd(self.work_dir if self.name else os.path.curdir):
            try:
                if self.name:
                    if self.components_common_conf['keep intermediate files']:
                        if os.path.isfile('conf.json'):
                            raise FileExistsError(
                                'Components configuration file "conf.json" already exists')
                        self.logger.debug('Create components configuration file "conf.json"')
                        with open('conf.json', 'w', encoding='ascii') as fp:
                            json.dump(self.components_common_conf, fp, sort_keys=True, indent=4)

                    core.utils.report(self.logger,
                                      'start',
                                      {
                                          'id': self.id,
                                          'parent id': self.parent['id'],
                                          'name': 'Sub-job',
                                          'attrs': [{'name': self.name}],
                                      },
                                      self.mqs['report files'],
                                      self.components_common_conf['main working directory'])

                # Specify callbacks to collect verification statuses from VTG. They will be used to
                # calculate validation and testing results.
                if 'ideal verdicts' in self.components_common_conf:
                    def before_launch_sub_job_components(context):
                        context.mqs['verification statuses'] = multiprocessing.Queue()

                    def after_generate_abstact_verification_task_desc(context):
                        if not context.abstract_task_desc_file:
                            context.mqs['verification statuses'].put({
                                "verification object": context.verification_obj,
                                "rule specification": context.rule_spec,
                                "verification status": 'unknown'
                            })

                    def after_process_single_verdict(context):
                        context.mqs['verification statuses'].put({
                            "verification object": context.conf['abstract task desc']['attrs'][0]['verification object'],
                            "rule specification": context.rule_specification,
                            "verification status": context.verification_status
                        })

                    def after_generate_all_verification_tasks(context):
                        context.logger.info('Terminate verification statuses message queue')
                        context.mqs['verification statuses'].put(None)

                    core.utils.set_component_callbacks(self.logger, type(self),
                                                       (
                                                           before_launch_sub_job_components,
                                                           after_generate_abstact_verification_task_desc,
                                                           after_process_single_verdict,
                                                           after_generate_all_verification_tasks
                                                       ))

                self.get_sub_job_components()

                self.callbacks = core.utils.get_component_callbacks(self.logger, [type(self)] + self.components,
                                                                    self.components_common_conf)

                self.launch_sub_job_components()
            except Exception:
                if self.name:
                    if self.mqs:
                        with open('problem desc.txt', 'w', encoding='ascii') as fp:
                            traceback.print_exc(file=fp)

                        if os.path.isfile('problem desc.txt'):
                            core.utils.report(self.logger,
                                              'unknown',
                                              {
                                                  'id': self.id + '/unknown',
                                                  'parent id': self.id,
                                                  'problem desc': 'problem desc.txt',
                                                  'files': ['problem desc.txt']
                                              },
                                              self.mqs['report files'],
                                              self.components_common_conf['main working directory'])

                    if self.logger:
                        self.logger.exception('Catch exception')
                    else:
                        traceback.print_exc()

                    self.logger.error(
                        'Decision of sub-job of type "{0}" with identifier "{1}" failed'.format(self.type, self.id))

                    # TODO: components.py makes this better. I hope that multiprocessing extensions implemented there
                    # will be used for sub-jobs as well one day.
                    sys.exit(1)
                else:
                    raise
            finally:
                core.utils.remove_component_callbacks(self.logger, type(self))

                if self.name:
                    core.utils.report(self.logger,
                                      'finish',
                                      {
                                          'id': self.id,
                                          'resources': {'wall time': 0, 'CPU time': 0, 'memory size': 0},
                                          'log': None
                                      },
                                      self.mqs['report files'],
                                      self.components_common_conf['main working directory'])

    def get_class(self):
        self.logger.info('Get job class')
        with open(self.CLASS_FILE, encoding='ascii') as fp:
            self.type = fp.read()
        self.logger.debug('Job class is "{0}"'.format(self.type))

    def get_common_components_conf(self, core_conf):
        self.logger.info('Get components common configuration')

        with open(core.utils.find_file_or_dir(self.logger, os.path.curdir, 'job.json'), encoding='ascii') as fp:
            self.components_common_conf = json.load(fp)

        # Add complete Klever Core configuration itself to components configuration since almost all its attributes will
        # be used somewhere in components.
        self.components_common_conf.update(core_conf)

        if self.components_common_conf['keep intermediate files']:
            if os.path.isfile('components common conf.json'):
                raise FileExistsError(
                    'Components common configuration file "components common conf.json" already exists')
            self.logger.debug('Create components common configuration file "components common conf.json"')
            with open('components common conf.json', 'w', encoding='ascii') as fp:
                json.dump(self.components_common_conf, fp, sort_keys=True, indent=4)

    def get_sub_jobs(self):
        self.logger.info('Get job sub-jobs')

        if 'Common' in self.components_common_conf and 'Sub-jobs' not in self.components_common_conf:
            raise KeyError('You can not specify common sub-jobs configuration without sub-jobs themselves')

        if 'Common' in self.components_common_conf:
            self.components_common_conf.update(self.components_common_conf['Common'])
            del (self.components_common_conf['Common'])

        if 'Sub-jobs' in self.components_common_conf:
            for sub_job_concrete_conf in self.components_common_conf['Sub-jobs']:
                # Sub-job configuration is based on common sub-jobs configuration.
                sub_job_components_common_conf = copy.deepcopy(self.components_common_conf)
                sub_job_concrete_conf = core.utils.merge_confs(sub_job_components_common_conf, sub_job_concrete_conf)

                self.logger.info('Get sub-job name and type')
                external_modules = sub_job_concrete_conf['Linux kernel'].get('external modules', '')

                modules = sub_job_concrete_conf['Linux kernel']['modules']
                if len(modules) == 1:
                    modules_hash = modules[0]
                else:
                    modules_hash = hashlib.sha1(''.join(modules).encode('utf8')).hexdigest()[:7]

                rule_specs = sub_job_concrete_conf['rule specifications']
                if len(rule_specs) == 1:
                    rule_specs_hash = rule_specs[0]
                else:
                    rule_specs_hash = hashlib.sha1(''.join(rule_specs).encode('utf8')).hexdigest()[:7]

                if self.type == 'Validation on commits in Linux kernel Git repositories':
                    commit = sub_job_concrete_conf['Linux kernel']['Git repository']['commit']
                    if len(commit) != 12 and (len(commit) != 13 or commit[12] != '~'):
                        raise ValueError(
                            'Commit hashes should have 12 symbols and optional "~" at the end ("{0}" is given)'.format(
                                commit))
                    sub_job_name_prefix = os.path.join(commit, external_modules)
                    sub_job_name = os.path.join(commit, external_modules, modules_hash, rule_specs_hash)
                    sub_job_work_dir = os.path.join(commit, external_modules, modules_hash,
                                                    re.sub(r'\W', '-', rule_specs_hash))
                    sub_job_type = 'Verification of Linux kernel modules'
                elif self.type == 'Verification of Linux kernel modules':
                    sub_job_name_prefix = os.path.join(external_modules)
                    sub_job_name = os.path.join(external_modules, modules_hash, rule_specs_hash)
                    sub_job_work_dir = os.path.join(external_modules, modules_hash, re.sub(r'\W', '-', rule_specs_hash))
                    sub_job_type = 'Verification of Linux kernel modules'
                else:
                    raise NotImplementedError('Job class "{0}" is not supported'.format(self.type))
                self.logger.debug('Sub-job name and type are "{0}" and "{1}"'.format(sub_job_name, sub_job_type))

                sub_job_id = self.id + sub_job_name

                for sub_job in self.sub_jobs:
                    if sub_job.id == sub_job_id:
                        raise ValueError('Several sub-jobs have the same identifier "{0}"'.format(sub_job_id))

                sub_job = Job(self.logger, sub_job_id, sub_job_type)
                self.sub_jobs.append(sub_job)
                sub_job.parent = {'id': self.id, 'type': self.type}
                sub_job.name_prefix = sub_job_name_prefix
                sub_job.name = sub_job_name
                sub_job.work_dir = sub_job_work_dir
                sub_job.mqs = self.mqs
                sub_job.locks = self.locks
                sub_job.uploading_reports_process = self.uploading_reports_process
                sub_job.data = self.data
                sub_job.data_lock = self.data_lock
                sub_job.components_common_conf = sub_job_concrete_conf

    def get_sub_job_components(self):
        self.logger.info('Get components for sub-job of type "{0}" with identifier "{1}"'.format(self.type, self.id))

        if self.type not in self.JOB_CLASS_COMPONENTS:
            raise NotImplementedError('Job class "{0}" is not supported'.format(self.type))

        self.components = [getattr(importlib.import_module('.{0}'.format(component.lower()), 'core'), component) for
                           component in self.JOB_CLASS_COMPONENTS[self.type]]

        self.logger.debug('Components to be launched: "{0}"'.format(
            ', '.join([component.__name__ for component in self.components])))

    def extract_archive(self):
        self.logger.info('Extract job archive "{0}" to directory "{1}"'.format(self.ARCHIVE, self.DIR))
        with tarfile.open(self.ARCHIVE) as TarFile:
            TarFile.extractall(self.DIR)

    def launch_sub_job_components(self):
        self.logger.info('Launch components for sub-job of type "{0}" with identifier "{1}"'.format(self.type, self.id))

        try:
            for component in self.components:
                p = component(self.components_common_conf, self.logger, self.id, self.callbacks, self.mqs,
                              self.locks, separate_from_parent=True)
                p.start()
                self.component_processes.append(p)

            # Every second check whether some component died. Otherwise even if some non-first component will die we
            # will wait for all components that preceed that failed component prior to notice that something went
            # wrong. Treat process that upload reports as component that may fail.
            while True:
                # The number of components that are still operating.
                operating_components_num = 0

                for p in self.component_processes:
                    p.join(1.0 / len(self.component_processes))
                    operating_components_num += p.is_alive()

                if not operating_components_num:
                    break

                if self.uploading_reports_process.exitcode:
                    raise RuntimeError('Uploading reports failed')
        except Exception:
            for p in self.component_processes:
                # Do not terminate components that already exitted.
                if p.is_alive():
                    p.stop()

            if 'verification statuses' in self.mqs:
                self.logger.info('Forcibly terminate verification statuses message queue')
                self.mqs['verification statuses'].put(None)

            raise
        finally:
            self.report_results()

    def report_results(self):
        if 'ideal verdicts' in self.components_common_conf:
            verification_statuses = []
            while True:
                verification_status = self.mqs['verification statuses'].get()

                if verification_status is None:
                    self.logger.debug('Verification statuses message queue was terminated')
                    self.mqs['verification statuses'].close()
                    del self.mqs['verification statuses']
                    break

                verification_statuses.append(verification_status)

            # There is no verification statuses when some (sub)component failed prior to VTG strategy
            # receives some abstract verification tasks.
            if not verification_statuses:
                verification_statuses.append({
                    'verification object': None,
                    'rule specification': None,
                    'verification status': 'unknown'
                })

            with self.data_lock:
                # Common processing of new results.
                self.data.update(self.__match_verification_statuses_and_ideal_verdicts(
                    verification_statuses, self.components_common_conf['ideal verdicts']))
                # Without this we won't be able to reliably iterate over data since it is
                # multiprocessing.Manager().dict().
                results = self.data.copy()

                if self.parent['type'] == 'Validation on commits in Linux kernel Git repositories':
                    results = self.process_validation_results(results)
                elif self.parent['type'] == 'Verification of Linux kernel modules':
                    results = self.process_testing_results(results)
                else:
                    raise NotImplementedError('Job class "{0}" is not supported'.format(self.parent['type']))

                core.utils.report(self.logger,
                                  'data',
                                  {
                                      'id': self.parent['id'],
                                      'data': json.dumps(results)
                                  },
                                  self.mqs['report files'],
                                  self.components_common_conf['main working directory'])

    def process_testing_results(self, results):
        self.logger.info('Check whether tests passed')

        for test in results:
            self.logger.info('Expected/obtained verification status for test "{0}" is "{1}"/"{2}"{3}'.format(
                test, results[test]['ideal verdict'], results[test]['verification status'],
                ' ("{0}")'.format(results[test]['comment']) if results[test]['comment'] else ''))

        # Report results as is.
        return results

    def process_validation_results(self, results):
        self.logger.info('Relate validation results on commits before and after corresponding bug fixes if so')

        new_results = {}

        bug_results = {}
        bug_fix_results = {}
        for name, results in results.items():
            # Corresponds to validation result before bug fix.
            if results['ideal verdict'] == 'unsafe':
                bug_results[name] = results
            # Corresponds to validation result after bug fix.
            elif results['ideal verdict'] == 'safe':
                bug_fix_results[name] = results
            else:
                raise ValueError(
                    'Ideal verdict is "{0}" (either "safe" or "unsafe" is expected)'.format(results['ideal verdict']))

        for bug_id, bug_result in bug_results.items():
            found_bug_fix_result = None
            for bug_fix_id, bug_fix_result in bug_fix_results.items():
                # Commit hash before/after corresponding bug fix is considered to be "hash~"/"hash" or v.v. Also it is
                # taken into account that all commit hashes have exactly 12 symbols.
                if bug_id[:12] == bug_fix_id[:12] and (bug_id[13:] == bug_fix_id[12:]
                                                       if len(bug_id) > 12 and bug_id[12] == '~'
                                                       else bug_id[12:] == bug_fix_id[13:]):
                    found_bug_fix_result = bug_fix_result
                    break

            validation_res_msg = 'Verification status for bug "{0}" before fix is "{1}"{2}'.format(
                bug_id, bug_result['verification status'],
                ' ("{0}")'.format(bug_result['comment'])
                if bug_result['comment']
                else '')

            # At least save validation result before bug fix.
            new_results[bug_id] = {'before fix': bug_result}

            if not found_bug_fix_result:
                self.logger.warning('Could not find validation result after fix of bug "{0}"'.format(bug_id))
                new_results[bug_id]['after fix'] = None
            else:
                validation_res_msg += ', after fix is "{0}"{1}'.format(found_bug_fix_result['verification status'],
                                                                       ' ("{0}")'.format(
                                                                           found_bug_fix_result['comment'])
                                                                       if found_bug_fix_result['comment'] else '')
                new_results[bug_id]['after fix'] = found_bug_fix_result

            self.logger.info(validation_res_msg)

        return new_results

    def __match_verification_statuses_and_ideal_verdicts(self, verification_statuses, ideal_verdicts):
        results = {}

        for verification_status in verification_statuses:
            verification_object = verification_status['verification object']
            rule_specification = verification_status['rule specification']
            verification_status = verification_status['verification status']

            if verification_object and rule_specification:
                # Refine name (it can contain hashes if several modules or/and rule specifications are checked within
                # one sub-job).
                name = os.path.join(self.name_prefix, verification_object, rule_specification)
            else:
                name = self.name_prefix

            # Try to match exactly by both verification object and rule specification.
            for ideal_verdict in ideal_verdicts:
                if 'verification object' in ideal_verdict and 'rule specification' in ideal_verdict \
                        and ideal_verdict['verification object'] == verification_object \
                        and ideal_verdict['rule specification'] == rule_specification:
                    results[name] = {
                        'ideal verdict': ideal_verdict['ideal verdict'],
                        'verification status': verification_status,
                        'comment': ideal_verdict.get('comment')
                    }
                    break

            # Try to match just by verification object.
            if name not in results:
                for ideal_verdict in ideal_verdicts:
                    if 'verification object' in ideal_verdict and 'rule specification' not in ideal_verdict \
                            and ideal_verdict['verification object'] == verification_object:
                        results[name] = {
                            'ideal verdict': ideal_verdict['ideal verdict'],
                            'verification status': verification_status,
                            'comment': ideal_verdict.get('comment')
                        }
                        break

            # Try to match just by rule specification.
            if name not in results:
                for ideal_verdict in ideal_verdicts:
                    if 'verification object' not in ideal_verdict and 'rule specification' in ideal_verdict \
                            and ideal_verdict['rule specification'] == rule_specification:
                        results[name] = {
                            'ideal verdict': ideal_verdict['ideal verdict'],
                            'verification status': verification_status,
                            'comment': ideal_verdict.get('comment')
                        }
                        break

            # If nothing of above matched.
            if name not in results:
                for ideal_verdict in ideal_verdicts:
                    if 'verification object' not in ideal_verdict and 'rule specification' not in ideal_verdict:
                        results[name] = {
                            'ideal verdict': ideal_verdict['ideal verdict'],
                            'verification status': verification_status,
                            'comment': ideal_verdict.get('comment')
                        }
                        break

            if name not in results:
                raise ValueError('Could not find appropriate ideal verdict for verification status "{0}", '
                                 'verification object "{1}" and rule specification "{2}"'.
                                 format(verification_status, verification_object, rule_specification))

        return results
