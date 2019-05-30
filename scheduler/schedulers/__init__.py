#
# Copyright (c) 2018 ISP RAS (http://www.ispras.ru)
# Ivannikov Institute for System Programming of the Russian Academy of Sciences
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

import os
import time
import traceback
import json
import pika
import re

import server.testgenerator as testgenerator
import server.bridge as bridge
from utils import sort_priority, time_units_converter, memory_units_converter


class SchedulerException(RuntimeError):
    """Exception is used to determine when task or job fails but not scheduler."""
    pass


class Scheduler:
    """Class provide general scheduler API."""

    def __init__(self, conf, logger, work_dir, runner_class):
        """
        Get configuration and prepare working directory.

        :param conf: Dictionary with relevant configuration.
        :param logger: Logger object.
        :param work_dir: Path to the working directory.
        :param runner_class: Runner class to work with hardware or cloud.
        """
        # todo: remove useless data
        self.conf = conf
        self.logger = logger
        self.work_dir = work_dir
        self.runner_class = runner_class
        self.__tasks = {}
        self.__jobs = {}
        self.__nodes = None
        self.__tools = None
        self.__iteration_period = {
            "short": 5,
            "medium": 10,
            "long": 20
        }
        self.__last_exchange = None
        self.__current_period = None
        self.production = self.conf["scheduler"].setdefault("production", False)
        self.init_scheduler()
        self.logger.info("Scheduler base initialization has been successful")

    def init_scheduler(self):
        """
        Initialize scheduler completely. This method should be called both at constructing stage and scheduler
        reinitialization. Thus, all object attribute should be cleaned up and set as it is a newly created object.
        """
        self.__tasks = {}
        self.__jobs = {}
        self.__nodes = None
        self.__tools = None
        self.__iteration_period = {
            "short": 5,
            "medium": 10,
            "long": 20
        }
        self.__last_exchange = None
        # todo: remove test generator at all finally
        if "debug with testgenerator" in self.conf["scheduler"] and self.conf["scheduler"]["debug with testgenerator"]:
            self.server = testgenerator.Server(self.logger, self.conf["testgenerator"],
                                               os.path.join(self.work_dir, "requests"))
        else:
            self.server = bridge.Server(self.logger, self.conf["Klever Bridge"],
                                        os.path.join(self.work_dir, "requests"))
            self.channel = pika.BlockingConnection(pika.ConnectionParameters(
                host=self.conf["Klever jobs and tasks queue"]["host"],
                credentials=pika.credentials.PlainCredentials(
                    self.conf["Klever jobs and tasks queue"]["username"],
                    self.conf["Klever jobs and tasks queue"]["password"]
                )
            )).channel()
            self.channel.queue_declare(queue=self.conf["Klever jobs and tasks queue"]["name"], durable=True)

        _old_tasks_status = None
        _old_jobs_status = None

        # Check configuration completeness
        self.logger.debug("Check whether configuration contains all necessary data")

        # Initialize interaction
        self.server.register(self.runner_class.scheduler_type())

        # todo: remove
        if "iteration timeout" in self.conf["scheduler"]:
            for tag in (t for t in self.__iteration_period.keys() if t in self.conf["scheduler"]["iteration timeout"]):
                self.__iteration_period[tag] = self.conf["scheduler"]["iteration timeout"][tag]
        self.__current_period = self.__iteration_period['short']

        constructor = self.runner_class
        self.runner = constructor(self.conf, self.logger, self.work_dir, self.server)
        self.runner.init()
        self.logger.info("Scheduler base initialization has been successful")

    def launch(self):
        """
        Start scheduler loop. This is an infinite loop that exchange data with Bridge to fetch new jobs and tasks and
        upload result of solution previously received tasks and jobs. After data exchange it prepares for solution
        new jobs and tasks, updates statuses of running jobs and tasks and schedule for solution pending ones.
        This is just an algorythm, and all particular logic and resource management should be implemented in classes
        that inherits this one.
        """
        def new_status(a, b):
            return any(len({a[i], b[i]}) != 1 for i in range(len(a)))
        _old_task_status = None
        _old_job_status = None

        # For shorter expressions
        jbs = self.__jobs
        tks = self.__tasks
        
        transition_done = False
        self.logger.info("Start scheduler loop")
        to_cancel = set()
        while True:
            # todo: implement this not in one function but in corresponding methods. Here implement just the switch
            def callback(ch, method, properties, body):
                # TODO: here should be all processing of information on jobs and tasks from Bridge.
                self.logger.info('Read: {0}'.format(body.decode('utf-8')))
                kind = None
                identifier = None
                status = None
                try:
                    kind, identifier, status = re.fullmatch("(\w+) ((?:\w|-)+)\: (\d+)", body.decode('utf-8')).groups()
                except AttributeError:
                    self.logger.warning("Cannot parse message!")

                if kind == 'job':
                    if status == '1':
                        job_conf = self.server.pull_job_conf(identifier)
                        # TODO: This is useless step
                        job_conf['configuration']['task resource limits'] = job_conf['tasks']

                        self.logger.debug("Add new PENDING job {}".format(identifier))
                        jbs[identifier] = {
                            "id": identifier,
                            "status": "PENDING",
                            "configuration": job_conf['configuration']
                        }

                        # Prepare jobs before launching
                        self.logger.debug("Prepare new job {} before launching".format(identifier))

                        # Check and set necessary restrictions for further scheduling
                        for collection in [jbs[identifier]["configuration"]["resource limits"],
                                           jbs[identifier]["configuration"]["task resource limits"]]:
                            try:
                                self.__add_missing_restrictions(collection)
                            except SchedulerException as err:
                                jbs[identifier] = {
                                    "id": identifier,
                                    "status": "ERROR",
                                    "error": str(err)
                                }
                                break
                        else:
                            self.runner.prepare_job(identifier, jbs[identifier])
                            try:
                                self.runner.solve_job(identifier, jbs[identifier])
                            except Exception:
                                self.logger.warning('Job terminated')
                    elif status == '6':
                        self.server.cancel_job(identifier)
                    else:
                        raise NotImplementedError

            # todo: We have the following problems here:
            # todo: Move sending tools and node data to controller - here now we cannot do periodical tasks
            # todo: Rethink how to check that a job or a task is finised. Who will check future object status?
            # todo: Rescheduling actions? How it should be done?
            self.channel.basic_consume(queue=self.conf["Klever jobs and tasks queue"]["name"],
                                       on_message_callback=callback,
                                       auto_ack=True)
            self.channel.start_consuming()

            try:
                # sch_ste = {
                #     "tasks": {
                #         "pending": [task_id for task_id in tks if "status" in tks[task_id] and
                #                     tks[task_id]["status"] == "PENDING" and not tks[task_id].get("rescheduled")],
                #         "processing": [task_id for task_id in tks if "status" in tks[task_id] and
                #                        tks[task_id]["status"] == "PROCESSING" or
                #                        (tks[task_id]["status"] == "PENDING" and
                #                         tks[task_id].get("rescheduled"))],
                #         "finished": [task_id for task_id in tks if "status" in tks[task_id] and
                #                      tks[task_id]["status"] == "FINISHED"],
                #         "error": [task_id for task_id in tks if "status" in tks[task_id] and
                #                   tks[task_id]["status"] == "ERROR"]
                #     },
                # }
                # status = (len(sch_ste["tasks"]["pending"]), len(sch_ste["tasks"]["processing"]),
                #           len(sch_ste["tasks"]["finished"]), len(sch_ste["tasks"]["error"]))
                # if not _old_task_status or new_status(status, _old_task_status):
                #     self.logger.info("Scheduler has {} pending, {} processing, {} finished and {} error tasks".
                #                      format(*status))
                #     _old_task_status = status
                # sch_ste["jobs"] = {
                #     "pending": [job_id for job_id in jbs if "status" in jbs[job_id] and
                #                 jbs[job_id]["status"] == "PENDING"],
                #     "processing": [job_id for job_id in jbs if "status" in jbs[job_id] and
                #                    jbs[job_id]["status"] == "PROCESSING"],
                #     "finished": [job_id for job_id in jbs if "status" in jbs[job_id] and
                #                  jbs[job_id]["status"] == "FINISHED"],
                #     "error": [job_id for job_id in jbs if "status" in jbs[job_id] and
                #               jbs[job_id]["status"] == "ERROR"],
                #     "cancelled": list(to_cancel)
                # }
                # # Update
                # status = (len(sch_ste["jobs"]["pending"]), len(sch_ste["jobs"]["processing"]),
                #           len(sch_ste["jobs"]["finished"]), len(sch_ste["jobs"]["error"]), len(to_cancel))
                # if not _old_job_status or new_status(status, _old_job_status):
                #     self.logger.info("Scheduler has {} pending, {} processing, {} finished and {} error jobs and {} "
                #                      "cancelled".format(*status))
                #     _old_job_status = status
                # if len(to_cancel) > 0:
                #     transition_done = True

                # Add task errors
                # if len(sch_ste["tasks"]["error"]) > 0:
                #     self.logger.info("Add task {} error descriptions".format(len(sch_ste["tasks"]["error"])))
                #     sch_ste["task errors"] = {}
                #     for task_id in sch_ste["tasks"]["error"]:
                #         sch_ste["task errors"][task_id] = str(tks[task_id]["error"])
                #
                # # Add jobs errors
                # if len(sch_ste["jobs"]["error"]) > 0:
                #     self.logger.info("Add job {} error descriptions".format(len(sch_ste["jobs"]["error"])))
                #     sch_ste["job errors"] = {}
                #     for job_id in sch_ste["jobs"]["error"]:
                #         sch_ste["job errors"][job_id] = str(jbs[job_id]["error"])

                # todo: Currently we do not send anything back
                # # Submit scheduler state and receive server state
                # if transition_done or self.__need_exchange:
                #     # Prepare scheduler state
                #     self.logger.debug("Start scheduling iteration with statuses exchange with the server")
                #     transition_done = False
                #     to_cancel = set()
                #     ser_ste = self.server.exchange(sch_ste)
                #     self.__last_exchange = int(time.time())
                #     try:
                #         # Ignore tasks which have been finished or cancelled
                #         for task_id in [task_id for task_id in tks if tks[task_id]["status"] in ["FINISHED", "ERROR"]]:
                #             if task_id in ser_ste["tasks"]["pending"]:
                #                 self.logger.debug("Ignore PENDING task {}, since it has been processed recently".
                #                                   format(task_id))
                #                 ser_ste["tasks"]["pending"].remove(task_id)
                #             if task_id in ser_ste["tasks"]["processing"]:
                #                 self.logger.debug("Ignore PROCESSING task {}, since it has been processed recently")
                #                 ser_ste["tasks"]["processing"].remove(task_id)
                #
                #         # Ignore jobs which have been finished or cancelled
                #         for job_id in [job_id for job_id in jbs if jbs[job_id]["status"] in ["FINISHED", "ERROR"]]:
                #             if job_id in ser_ste["jobs"]["pending"]:
                #                 self.logger.debug("Ignore PENDING job {}, since it has been processed recently".
                #                                   format(job_id))
                #                 ser_ste["jobs"]["pending"].remove(job_id)
                #             if job_id in ser_ste["jobs"]["processing"]:
                #                 self.logger.debug("Ignore PROCESSING job {}, since it has been processed recently")
                #                 ser_ste["jobs"]["processing"].remove(job_id)
                #     except KeyError as missed_tag:
                #         self.__report_error_server_state(ser_ste,
                #                                          "Missed tag {} in a received server state".format(missed_tag))
                #
                #     if 'jobs progress' in ser_ste:
                #         for job_id, progress in [(i, d) for i, d in ser_ste['jobs progress'].items() if i in jbs]:
                #             self.runner.add_job_progress(job_id, jbs[job_id], progress)
                #
                #     # Remove finished or error tasks which have been already submitted
                #     to_remove = set(sch_ste["tasks"]["finished"] + sch_ste["tasks"]["error"])
                #     if len(to_remove) > 0:
                #         self.logger.debug("Remove tasks with statuses FINISHED and ERROR which have been submitted")
                #         for task_id in to_remove:
                #             self.logger.debug("Delete task {} with status {}".format(task_id, tks[task_id]["status"]))
                #             del tks[task_id]
                #
                #     # Remove finished or error jobs
                #     to_remove = set(sch_ste["jobs"]["finished"] + sch_ste["jobs"]["error"])
                #     if len(to_remove) > 0:
                #         self.logger.debug("Remove jobs with statuses FINISHED and ERROR")
                #         for job_id in to_remove:
                #             self.logger.debug("Delete job {} with status {}".format(job_id, jbs[job_id]["status"]))
                #             del jbs[job_id]
                #
                #     # Add new PENDING tasks
                #     for task_id in [task_id for task_id in ser_ste["tasks"]["pending"] if task_id not in tks]:
                #         self.logger.debug("Add new PENDING task {}".format(task_id))
                #         try:
                #             tks[task_id] = {
                #                 "id": task_id,
                #                 "status": "PENDING",
                #                 "description": ser_ste["task descriptions"][task_id]["description"],
                #                 "priority": ser_ste["task descriptions"][task_id]["description"]["priority"]
                #             }
                #
                #             # TODO: VerifierCloud user name and password are specified in task description and
                #             # shouldn't be extracted from it here.
                #             if self.runner.scheduler_type() == "VerifierCloud":
                #                 tks[task_id]["user"] = ser_ste["task descriptions"][task_id]["VerifierCloud user name"]
                #                 tks[task_id]["password"] = \
                #                     ser_ste["task descriptions"][task_id]["VerifierCloud user password"]
                #             else:
                #                 tks[task_id]["user"] = None
                #                 tks[task_id]["password"] = None
                #         except KeyError as missed_tag:
                #             self.__report_error_server_state(
                #                 ser_ste, "Missed tag '{}' in the description of pendng task {}".format(missed_tag,
                #                                                                                        task_id))
                #
                #         # Try to prepare task
                #         self.logger.debug("Prepare new task {} before launching".format(task_id))
                #         # Add missing restrictions
                #         try:
                #             self.__add_missing_restrictions(tks[task_id]["description"]["resource limits"])
                #         except SchedulerException as err:
                #             jbs[task_id] = {
                #                 "id": task_id,
                #                 "status": "ERROR",
                #                 "error": str(err)
                #             }
                #         else:
                #             self.runner.prepare_task(task_id, tks[task_id])
                #
                #     # Add new PENDING jobs
                #     for job_id in [job_id for job_id in ser_ste["jobs"]["pending"] if job_id not in jbs]:
                #         self.logger.debug("Add new PENDING job {}".format(job_id))
                #         jbs[job_id] = {
                #             "id": job_id,
                #             "status": "PENDING",
                #             "configuration": ser_ste["job configurations"][job_id]
                #         }
                #
                #         # Prepare jobs before launching
                #         self.logger.debug("Prepare new job {} before launching".format(job_id))
                #
                #         # Check and set necessary restrictions for further scheduling
                #         for collection in [jbs[job_id]["configuration"]["resource limits"],
                #                            jbs[job_id]["configuration"]["task resource limits"]]:
                #             try:
                #                 self.__add_missing_restrictions(collection)
                #             except SchedulerException as err:
                #                 jbs[job_id] = {
                #                     "id": job_id,
                #                     "status": "ERROR",
                #                     "error": str(err)
                #                 }
                #                 break
                #         else:
                #             self.runner.prepare_job(job_id, jbs[job_id])
                #
                #     # Cancel tasks
                #     for task_id in [task_id for task_id in
                #                     set(sch_ste["tasks"]["pending"] + sch_ste["tasks"]["processing"]) if
                #                     task_id not in set(ser_ste["tasks"]["pending"] + sch_ste["tasks"]["processing"])]:
                #         self.logger.debug("Cancel task {} with status {}".
                #                           format(task_id, tks[task_id]['status']))
                #         self.runner.cancel_task(task_id, tks[task_id])
                #         del tks[task_id]
                #         if not transition_done:
                #             transition_done = True
                #
                #     # Cancel jobs
                #     for job_id in [job_id for job_id in jbs if jbs[job_id]["status"] in ["PENDING", "PROCESSING"] and
                #                    (job_id not in set(ser_ste["jobs"]["pending"] + ser_ste["jobs"]["processing"])
                #                     or job_id in ser_ste["jobs"]["cancelled"])]:
                #         self.logger.debug("Cancel job {} with status {}".format(job_id, jbs[job_id]['status']))
                #         self.runner.cancel_job(
                #             job_id, jbs[job_id],
                #             [tks[tid] for tid in tks if tks[tid]["status"] in ["PENDING", "PROCESSING"]
                #              and tks[tid]["description"]["job id"] == job_id])
                #
                #         del jbs[job_id]
                #         if not transition_done:
                #             transition_done = True
                #
                #         if job_id in ser_ste["jobs"]["cancelled"]:
                #             to_cancel.add(job_id)
                #
                #     # Add confirmation if necessary
                #     to_cancel.update({j for j in ser_ste["jobs"]["cancelled"] if j not in to_cancel and j not in jbs})
                #
                #     # Update jobs processing status
                #     for job_id in ser_ste["jobs"]["processing"]:
                #         if job_id in jbs:
                #             if self.runner.is_solving(jbs[job_id]) and jbs[job_id]["status"] == "PENDING":
                #                 jbs[job_id]["status"] = "PROCESSING"
                #                 if not transition_done:
                #                     transition_done = True
                #             elif not self.runner.is_solving(jbs[job_id]) or jbs[job_id]["status"] != "PROCESSING":
                #                 raise ValueError("Scheduler has lost information about job {} with PROCESSING status.".
                #                                  format(job_id))
                #         else:
                #             self.logger.warning("Job {} has status PROCESSING but it was not running actually".
                #                                 format(job_id))
                #             jbs[job_id] = {
                #                 "id": job_id,
                #                 "status": "ERROR",
                #                 "error": "Job {} has status PROCESSING but it was not running actually".format(job_id)
                #             }
                #             if not transition_done:
                #                 transition_done = True
                #
                #     # Update tasks processing status
                #     for task_id in ser_ste["tasks"]["processing"]:
                #         if task_id in tks and \
                #                 (not (tks[task_id].get("rescheduled") and tks[task_id]["status"] == 'PENDING')
                #                  and (not self.runner.is_solving(tks[task_id]) or
                #                       tks[task_id]["status"] != "PROCESSING")):
                #                 raise ValueError("Scheduler has lost information about task {} with PROCESSING status.".
                #                                  format(task_id))
                #         elif task_id not in tks:
                #             self.logger.warning("Task {} has status PROCESSING but it was not running actually".
                #                                 format(task_id))
                #             tks[task_id] = {
                #                 "id": task_id,
                #                 "status": "ERROR",
                #                 "error": "task {} has status PROCESSING but it was not running actually".format(task_id)
                #             }
                #             if not transition_done:
                #                 transition_done = True

                # Update statuses
                for task_id in [task_id for task_id in tks if tks[task_id]["status"] == "PROCESSING"]:
                    if self.runner.process_task_result(task_id, tks[task_id]):
                        transition_done = True

                # Update jobs
                for job_id in [job_id for job_id in jbs if jbs[job_id]["status"] in ["PENDING", "PROCESSING"]]:
                    if self.runner.process_job_result(
                            job_id, jbs[job_id], [tid for tid in tks if tks[tid]["status"] in ["PENDING", "PROCESSING"]
                                                  and tks[tid]["description"]["job id"] == job_id]):
                        transition_done = True

                # Submit tools
                try:
                    self.logger.debug("Update information about available verification tools")
                    self.runner.update_tools()
                except Exception as err:
                    self.logger.warning('Cannot submit verification tools information: {}'.format(err))

                # Get actual information about connected nodes
                submit = True
                try:
                    self.logger.debug("Update information about connected nodes")
                    self.runner.update_nodes()
                except Exception as err:
                    self.logger.error("Cannot obtain information about connected nodes: {}".format(err))
                    submit = False

                if submit:
                    # Schedule new tasks
                    pending_tasks = [tks[task_id] for task_id in tks if tks[task_id]["status"] == "PENDING"]
                    pending_jobs = [jbs[job_id] for job_id in jbs if jbs[job_id]["status"] == "PENDING"
                                    and not self.runner.is_solving(jbs[job_id])]
                    pending_jobs = sorted(pending_jobs, key=lambda i: sort_priority(i['configuration']['priority']))
                    pending_tasks = sorted(pending_tasks, key=lambda i: sort_priority(i['description']['priority']))
                    tasks_to_start, jobs_to_start = self.runner.schedule(pending_tasks, pending_jobs)
                    if len(tasks_to_start) > 0 or len(jobs_to_start) > 0:
                        self.logger.info("Going to start {} new tasks and {} jobs".
                                         format(len(tasks_to_start), len(jobs_to_start)))

                        for job_id in jobs_to_start:
                            if self.runner.is_solving(jbs[job_id]) or jbs[job_id]["status"] != "PENDING":
                                raise ValueError("Attempt to scheduler running or processed job {}".format(job_id))
                            if self.runner.solve_job(job_id, jbs[job_id]):
                                transition_done = True

                        for task_id in tasks_to_start:
                            # This check is very helpful for debugging
                            self.runner.solve_task(task_id, tks[task_id])

                    # Flushing tasks
                    if len(tasks_to_start) > 0 or \
                            len([True for task_id in tks if tks[task_id]["status"] == "PROCESSING"]) > 0:
                        self.logger.debug("Flush submitted tasks and jobs")
                        self.runner.flush()
                else:
                    self.logger.warning(
                        "Do not run any tasks until actual information about the nodes will be obtained")

                self.logger.debug("Scheduler iteration has finished")
                if not transition_done:
                    self.__update_iteration_period()
                    time.sleep(self.__iteration_period['short'])
                else:
                    self.logger.info("Do not wait besause of statuses changing")
                    time.sleep(1)
                # todo: Add termination of all jobs and statuses and sending the update to Bridge
            except KeyboardInterrupt:
                self.logger.error("Scheduler execution is interrupted, cancel all running threads")
                self.terminate()
                self.server.stop()
                exit(137)
            except Exception:
                exception_info = 'An error occured:\n{}'.format(traceback.format_exc().rstrip())
                self.logger.error(exception_info)
                self.terminate()
                if self.production:
                    self.logger.info("Reinitialize scheduler and try to proceed execution in 30 seconds...")
                    self.server.stop()
                    time.sleep(30)
                    self.init_scheduler()
                else:
                    self.server.stop()
                    exit(1)

    @staticmethod
    def __add_missing_restrictions(collection):
        """
        If resource limits are incomplete the method adds to given json all necessary fields filled with zeroes.

        :param collection: 'resource limits' dictionary from a task description or job configuration.
        """
        if len(collection.keys()) == 0:
            raise SchedulerException("Resource limitations are missing: upload correct tasks.json file and properly "
                                     "set job resource limitiations")

        for tag in ['memory size', 'number of CPU cores', 'disk memory size']:
            if tag not in collection or collection[tag] is None:
                collection[tag] = 0
        if 'CPU model' not in collection:
            collection['CPU model'] = None

        # Make unit translation
        try:
            for tag in (m for m in ("memory size", "disk memory size")
                        if m in collection and collection[m] is not None):
                collection[tag] = memory_units_converter(collection[tag])[0]
            for tag in (t for t in ("wall time", "CPU time") if t in collection and collection[t] is not None):
                collection[tag] = time_units_converter(collection[tag])[0]
        except Exception:
            raise SchedulerException('Cannot interprete {} resource limitations: {!r}'.format(tag, collection[tag]))

    def terminate(self):
        # TODO: Need refactoring! Send requests to Bridge
        """Abort solution of all running tasks and any other actions before termination."""
        # stop jobs
        for job_id, item in [(job_id, self.__jobs[job_id]) for job_id in self.__jobs
                             if self.__jobs[job_id]["status"] in ["PENDING", "PROCESSING"]]:
            relevant_tasks = [self.__tasks[tid] for tid in self.__tasks
                              if self.__tasks[tid]["status"] in ["PENDING", "PROCESSING"]
                              and self.__tasks[tid]["description"]["job id"] == job_id]
            self.runner.cancel_job(job_id, item, relevant_tasks)

        # Note here that some schedulers can solve tasks of jobs which run elsewhere
        for task_id, item in [(task_id, self.__tasks[task_id]) for task_id in self.__tasks
                              if self.__tasks[task_id]["status"] in ["PENDING", "PROCESSING"]]:
            self.runner.cancel_task(task_id, item)

        # Do final unitializations
        self.runner.terminate()
