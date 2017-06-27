import copy
import re

from core.avtg.emg.common.signature import import_declaration
from core.avtg.emg.common.interface import Interface, Callback, Container, Resource
from core.avtg.emg.common.process import Access, Process, Label, Call, CallRetval, Dispatch, Receive, Condition, \
    rename_subprocess


class ProcessModel:

    def __init__(self, logger, conf, models, processes, roles_map):
        self.logger = logger
        self.conf = conf
        self.__abstr_model_processes = models
        self.__abstr_event_processes = processes
        self.__roles_map = dict()
        self.__functions_map = dict()

        if "roles map" in roles_map:
            self.__roles_map = roles_map["roles map"]
        if "functions map" in roles_map:
            self.__functions_map = roles_map["functions map"]
            for type in self.__functions_map:
                self.__functions_map[type] = [re.compile(pattern) for pattern in self.__functions_map[type]]

        self.model_processes = []
        self.event_processes = []
        self.entry_process = None

    def generate_event_model(self, analysis):
        # todo: should work with multi-module analysis (issues #6558, #6563)
        self.logger.info("Generate model processes for Init and Exit module functions")
        self.__generate_entry(analysis)

        # Generate intermediate model
        self.logger.info("Generate an intermediate model")
        self.__select_processes_and_models(analysis)

        # Finish entry point process generation
        self.__finish_entry(analysis)

        # Convert callback access according to container fields
        self.logger.info("Determine particular interfaces and their implementations for each label or its field")
        self.__resolve_accesses(analysis)

    def __generate_entry(self, analysis):
        # todo: Implement multimodule processes creation (issues #6563, #6571, #6558)
        self.logger.info("Generate artificial process description to call Init and Exit module functions 'EMGentry'")
        ep = Process("EMGentry")
        ep.category = "entry"
        ep.identifier = 0
        self.entry_process = ep

        if len(analysis.inits) == 0:
            raise RuntimeError('Module does not have Init function')

        # Generate init subprocess
        for filename, init_name in analysis.inits:
            # todo: Add none instead of particular name (relevant to #6571)
            init_label = Label('init_{}'.format(init_name))
            init_label.value = "& {}".format(init_name)
            init_label.prior_signature = import_declaration("int (*f)(void)")
            init_label.file = filename
            init_subprocess = Call(init_label.name)
            init_subprocess.callback = "%init_{}%".format(init_name)
            init_subprocess.retlabel = "%ret%"
            init_subprocess.post_call = [
                '%ret% = ldv_post_init(%ret%);'
            ]
            self.logger.debug("Found init function {}".format(init_name))
            ep.labels[init_label.name] = init_label
            ep.actions[init_label.name] = init_subprocess

        ret_label = Label('ret')
        ret_label.prior_signature = import_declaration("int label")
        ep.labels['ret'] = ret_label

        # Generate exit subprocess
        if len(analysis.exits) == 0:
            # todo: Add none instead of particular name (relevant to #6571)
            self.logger.debug("There is no exit function found")
            exit_subprocess = Call('exit')
            exit_subprocess.callback = "%exit%"

            exit_label = Label('exit')
            exit_label.prior_signature = import_declaration("void (*f)(void)")
            exit_label.value = None
            exit_label.file = None
            ep.labels['exit'] = exit_label
            ep.actions['exit'] = exit_subprocess
        else:
            for filename, exit_name in analysis.exits:

                exit_label = Label('exit_{}'.format(exit_name))
                exit_label.prior_signature = import_declaration("void (*f)(void)")
                exit_label.value = "& {}".format(exit_name)
                exit_label.file = filename
                exit_subprocess = Call(exit_label.name)
                exit_subprocess.callback = "%exit_{}%".format(exit_name)
                self.logger.debug("Found exit function {}".format(exit_name))
                ep.labels[exit_label.name] = exit_label
                ep.actions[exit_label.name] = exit_subprocess

        self.logger.debug("Artificial process for invocation of Init and Exit module functions is generated")

    def __finish_entry(self, analysis):
        self.logger.info("Add signal dispatched for that processes which have no known registration and deregistration"
                         " kernel functions")
        # Retval check
        # todo: it can be done much, much better (relevant to issue #6566)
        dispatches = []
        # All default registrations and then deregistrations
        names = [name for name in sorted(self.entry_process.actions.keys())
                 if type(self.entry_process.actions[name]) is Dispatch]
        for name in names:
            self.entry_process.actions[name].broadcast = True
        names.sort()
        names.reverse()
        names[len(names):] = reversed(names[len(names):])
        dispatches.extend(["[@{}]".format(name) for name in names])

        # Generate conditions
        success = Condition('init_success')
        success.condition = ["%ret% == 0"]
        self.entry_process.actions['init_success'] = success

        failed = Condition('init_failed')
        failed.condition = ["%ret% != 0"]
        self.entry_process.actions['init_failed'] = failed

        stop = Condition('stop')
        stop.statements = ["ldv_check_final_state();",
                           "ldv_stop();"]
        self.entry_process.actions['stop'] = stop

        none = Condition('none')
        none.type = "condition"
        self.entry_process.actions['none'] = none

        # Add subprocesses finally
        self.entry_process.process = ""
        for i, pair in enumerate(analysis.inits):
            self.entry_process.process += "[init_{0}].(<init_failed>.".format(pair[1])
            for j, pair2 in enumerate(analysis.exits[::-1]):
                if pair2[0] == pair[0]:
                    break
            j = 1
            for _, exit_name in analysis.exits[:j-1:-1]:
                self.entry_process.process += "[exit_{}].".format(exit_name)
            self.entry_process.process += "<stop>|<init_success>."
        if len(dispatches):
            self.entry_process.process += "({}|<none>).".format(".".join(dispatches))
        else:
            self.entry_process.process += "<none>."
        for _, exit_name in analysis.exits:
            self.entry_process.process += "[exit_{}].".format(exit_name)
        self.entry_process.process += "<stop>"
        self.entry_process.process += ")" * len(analysis.inits)

    def __select_processes_and_models(self, analysis):
        # Import necessary kernel models
        self.logger.info("First, add relevant models of kernel functions")
        self.__import_kernel_models(analysis)

        for category in analysis.categories:
            uncalled_callbacks = analysis.uncalled_callbacks(category)
            self.logger.debug("There are {} callbacks in category {}".format(len(uncalled_callbacks), category))

            if uncalled_callbacks:
                self.logger.info("Try to find processes to call callbacks from category {}".format(category))
                new = self.__choose_processes(analysis, category)

                # Sanity check
                self.logger.info("Check again how many callbacks are not called still in category {}".format(category))
                uncalled_callbacks = analysis.uncalled_callbacks(category)
                if uncalled_callbacks and not ('ignore missed callbacks' in self.conf and
                                               self.conf['ignore missed callbacks']):
                    names = str([callback.identifier for callback in uncalled_callbacks])
                    raise RuntimeError("There are callbacks from category {} which are not called at all in the "
                                       "model: {}".format(category, names))
                elif uncalled_callbacks:
                    names = str([callback.identifier for callback in uncalled_callbacks])
                    self.logger.warning("There are callbacks from category {} which are not called at all in the "
                                        "model: {}. Disable option 'ignore missed callbacks' in intermediate model "
                                        "configuration properties if you would like to terminate.".
                                        format(category, names))

                if len(new.unmatched_dispatches) > 0 or len(new.unmatched_receives) > 0:
                    self.logger.info("Added process {} have unmatched signals, need to find factory or registration "
                                     "and deregistration functions".format(new.name))
                    self.__establish_signal_peers(analysis, new)
            else:
                self.logger.info("Ignore interface category {}, since it does not have callbacks to call".
                                 format(category))

    def __import_kernel_models(self, analysis):
        for function in sorted(list(self.__abstr_model_processes.keys())):
            if function in sorted(analysis.kernel_functions.keys()):
                self.logger.debug("Add model of function '{}' to an environment model".format(function))
                new_process = self.__add_process(analysis, self.__abstr_model_processes[function], model=True)
                new_process.category = "kernel models"

                self.logger.debug("Assign label interfaces according to function parameters for added process {} "
                                  "with an identifier {}".format(new_process.name, new_process.identifier))
                for label in sorted(new_process.labels.keys()):
                    # Assign label-parameters
                    if new_process.labels[label].parameter and not new_process.labels[label].prior_signature:
                        for index in range(len(analysis.kernel_functions[function].param_interfaces)):
                            parameter = analysis.kernel_functions[function].param_interfaces[index]
                            signature = analysis.kernel_functions[function].declaration.parameters[index]

                            if parameter and parameter.identifier in new_process.labels[label].interfaces:
                                self.logger.debug("Set label {} signature according to interface {}".
                                                  format(label, parameter.identifier))
                                new_process.labels[label].set_declaration(parameter.identifier, signature)
                                break
                    elif not new_process.labels[label].parameter and new_process.labels[label].interfaces:
                        for interface in new_process.labels[label].interfaces:
                            new_process.labels[label].set_declaration(interface,
                                                                      analysis.interfaces[interface].declaration)

                    if new_process.labels[label].parameter and len(new_process.labels[label].interfaces) == 0:
                        raise ValueError("Cannot find a suitable signature for a label '{}' at function model '{}'".
                                         format(label, function))

                    # Asssign rest parameters
                    if new_process.labels[label].interfaces and len(new_process.labels[label].interfaces) > 0:
                        for interface in (i for i in new_process.labels[label].interfaces
                                          if i in analysis.interfaces):
                            self.__assign_label_interface(new_process.labels[label], analysis.interfaces[interface])

    def __choose_processes(self, analysis, category):
        estimations = {}
        for process in [self.__abstr_event_processes[name] for name in sorted(self.__abstr_event_processes.keys())]:
            self.logger.debug("Estimate correspondence between  process {} and category {}".
                              format(process.name, category))
            estimations[process.name] = self.__match_labels(analysis, process, category)

        self.logger.info("Choose process to call callbacks from category {}".format(category))
        # First random
        best_process = self.__abstr_event_processes[[name for name in sorted(estimations) if estimations[name] and
                                                     len(estimations[name]["matched calls"]) > 0][0]]
        best_map = estimations[best_process.name]

        for process in [self.__abstr_event_processes[name] for name in sorted(estimations)]:
            label_map = estimations[process.name]
            if label_map and len(label_map["matched calls"]) > 0 and len(label_map["unmatched labels"]) == 0:
                self.logger.info("Matching process {} for category {}, it has:".format(process.name, category))
                self.logger.info("Matching labels: {}".format(str(label_map["matched labels"])))
                self.logger.info("Unmatched labels: {}".format(str(label_map["unmatched labels"])))
                self.logger.info("Matched callbacks: {}".format(str(label_map["matched callbacks"])))
                self.logger.info("Unmatched callbacks: {}".format(str(label_map["unmatched callbacks"])))
                self.logger.info("Matched calls: {}".format(str(label_map["matched calls"])))
                self.logger.info("Native interfaces: {}".format(str(label_map["native interfaces"])))

                do = False
                if label_map["native interfaces"] > best_map["native interfaces"]:
                    do = True
                elif best_map["native interfaces"] == 0:
                    if len(label_map["matched calls"]) > len(best_map["matched calls"]) and \
                            len(label_map["unmatched callbacks"]) <= len(best_map["unmatched callbacks"]):
                        do = True
                    elif len(label_map["matched calls"]) >= len(best_map["matched calls"]) and \
                            len(label_map["unmatched callbacks"]) <= len(best_map["unmatched callbacks"]) and \
                            len(label_map["unmatched labels"]) < len(best_map["unmatched labels"]):
                        do = True
                    elif len(label_map["unmatched callbacks"]) < len(best_map["unmatched callbacks"]):
                        do = True
                    else:
                        do = False

                if do:
                    best_map = label_map
                    best_process = process
                    self.logger.debug("Set process {} for category {} as the best one at the moment".
                                      format(process.name, category))

        if not best_process:
            raise RuntimeError("Cannot find suitable process in event categories specification for category {}"
                               .format(category))
        else:
            new = self.__add_process(analysis, best_process, category, False, best_map)
            new.category = category
            self.logger.debug("Finally choose process {} for category {} as the best one".
                              format(best_process.name, category))
            return new

    def __assign_label_interface(self, label, interface):
        if type(interface) is Container:
            label.set_declaration(interface.identifier, interface.declaration.take_pointer)
        else:
            label.set_declaration(interface.identifier, interface.declaration)

    def __add_process(self, analysis, process, category=None, model=False, label_map=None, peer=None):
        self.logger.info("Add process {} to the model".format(process.name))
        self.logger.debug("Make copy of process {} before adding it to the model".format(process.name))
        new = copy.deepcopy(process)
        if not category:
            new.category = 'kernel models'
        else:
            new.category = category

        # todo: Assign category for each new process not even for that which have callbacks (issue #6564)
        new.identifier = len(self.model_processes) + len(self.event_processes) + 1
        self.logger.info("Finally add process {} to the model".
                         format(process.name))

        self.logger.debug("Set interfaces for given labels")
        if label_map:
            for label in sorted(label_map["matched labels"].keys()):
                for interface in [analysis.interfaces[name] for name
                                  in sorted(label_map["matched labels"][label])
                                  if name in analysis.interfaces]:
                    self.__assign_label_interface(new.labels[label], interface)
        else:
            for label in new.labels.values():
                for interface in label.interfaces:
                    if not label.get_declaration(interface):
                        try:
                            self.__assign_label_interface(label, analysis.interfaces[interface])
                        except KeyError:
                            self.logger.warning("Process '{}' for category '{}' cannot be added, since it contains"
                                                "unknown interfaces for this verification object".
                                                format(new.name, new.category))
                            return None

        if model and not category:
            self.model_processes.append(new)
        elif not model and category:
            self.event_processes.append(new)
        else:
            raise ValueError('Provide either model or category arguments but not simultaneously')

        if peer:
            self.logger.debug("Match signals with signals of process {} with identifier {}".
                              format(peer.name, peer.identifier))
            new.establish_peers(peer)

        self.logger.info("Check is there exist any dispatches or receives after process addiction to tie".
                         format(process.name))
        self.__normalize_model(analysis)
        return new

    def __normalize_model(self, analysis):
        # Peer processes with models
        self.logger.info("Try to establish connections between process dispatches and receivings")
        for model in self.model_processes:
            for process in self.event_processes:
                self.logger.debug("Analyze signals of processes {} and {} in the model with identifiers {} and {}".
                                  format(model.name, process.name, model.identifier, process.identifier))
                model.establish_peers(process)

        # Peer processes with each other
        for index1 in range(len(self.event_processes)):
            p1 = self.event_processes[index1]
            for index2 in range(index1 + 1, len(self.event_processes)):
                p2 = self.event_processes[index2]
                self.logger.debug("Analyze signals of processes {} and {}".format(p1.name, p2.name))
                p1.establish_peers(p2)

        self.logger.info("Check which callbacks can be called in the intermediate environment model")
        for process in [process for process in self.model_processes] +\
                       [process for process in self.event_processes]:
            self.logger.debug("Check process callback calls at process {} with category {}".
                              format(process.name, process.category))

            for action in sorted(process.calls, key=lambda a: a.callback):
                # todo: refactoring #6565
                label, tail = process.extract_label_with_tail(action.callback)

                if len(label.interfaces) > 0:
                    resolved = False
                    for interface in [analysis.interfaces[name] for name in label.interfaces
                                      if name in analysis.interfaces and
                                      type(analysis.interfaces[name]) is not Resource]:
                        if type(interface) is Container and len(tail) > 0:
                            intfs = self.__resolve_interface(analysis, interface, tail, process, action)
                            if intfs:
                                intfs[-1].called = True
                                resolved = True
                            else:
                                self.logger.warning("Cannot resolve callback '{}' in description of process '{}'".
                                                    format(action.callback, process.name))
                        elif type(interface) is Callback:
                            self.logger.debug("Callback {} can be called in the model".
                                              format(interface.identifier))
                            interface.called = True
                            resolved = True
                    if not resolved:
                        raise ValueError("Cannot resolve callback '{}' in description of process '{}'".
                                         format(action.callback, process.name))

    def __add_label_match(self, label_map, label, interface):
        if label.name not in label_map["matched labels"]:
            self.logger.debug("Match label '{}' with interface '{}'".format(label.name, interface))
            label_map["matched labels"][label.name] = set([interface])
        else:
            label_map["matched labels"][label.name].add(interface)

    def __match_labels(self, analysis, process, category):
        self.logger.info("Try match process {} with interfaces of category {}".format(process.name, category))
        label_map = {
            "matched labels": {},
            "unmatched labels": [],
            "uncalled callbacks": [],
            "matched callbacks": [],
            "unmatched callbacks": [],
            "matched calls": [],
            "native interfaces": []
        }

        # Collect native categories and interfaces
        nc = set()
        ni = set()
        for label in [process.labels[name] for name in sorted(process.labels.keys())]:
            for intf in label.interfaces:
                intf_category, short_identifier = intf.split(".")
                nc.add(intf_category)

                if intf in analysis.interfaces and intf_category == category:
                    ni.add(intf)
                    self.__add_label_match(label_map, label, intf)
        label_map["native interfaces"] = len(ni)

        # Stop analysis if process tied with another category
        if len(nc) > 0 and len(ni) == 0:
            self.logger.debug("Process {} is intended to be matched with a category from the list: {}".
                              format(process.name, str(nc)))
            return None

        # todo: Code below is a bit greedy and it doesb't support arrays in access sequences
        # todo: better to rewrite it totally and implement comparison on the base of signatures
        success_on_iteration = True
        while success_on_iteration:
            success_on_iteration = False
            old_size = len(label_map["matched callbacks"]) + len(label_map["matched labels"])

            # Match interfaces and containers
            for action in process.calls:
                label, tail = process.extract_label_with_tail(action.callback)

                # Try to match container
                if len(label.interfaces) > 0 and label.name not in label_map["matched labels"]:
                    for interface in label.interfaces:
                        if interface in analysis.interfaces and analysis.interfaces[interface].category == category:
                            self.__add_label_match(label_map, label, interface)
                elif len(label.interfaces) == 0 and not label.prior_signature and tail and label.container and \
                        label.name not in label_map["matched labels"]:
                    for container in analysis.containers(category):
                        interfaces = self.__resolve_interface(analysis, container, tail, process, action)
                        if interfaces:
                            self.__add_label_match(label_map, label, container.identifier)

                # Try to match callback itself
                functions = []
                if label.name in label_map["matched labels"] and label.container:
                    for intf in sorted(label_map["matched labels"][label.name]):
                        intfs = self.__resolve_interface(analysis, analysis.interfaces[intf], tail, process, action)
                        if intfs and type(intfs[-1]) is Callback:
                            functions.append(intfs[-1])
                elif label.name in label_map["matched labels"] and label.callback:
                    if type(label_map["matched labels"][label.name]) is set:
                        functions.extend([analysis.interfaces[name] for name in
                                          sorted(label_map["matched labels"][label.name])
                                          if name in analysis.interfaces])
                    elif label_map["matched labels"][label.name] in analysis.interfaces:
                        functions.append(analysis.interfaces[label_map["matched labels"][label.name]])

                # Match parameters
                for function in functions:
                    labels = []
                    pre_matched = set()
                    for index in range(len(action.parameters)):
                        p_label, p_tail = process.extract_label_with_tail(action.parameters[index])
                        if p_tail:
                            for container in analysis.containers(category):
                                interfaces = self.__resolve_interface(analysis, container, p_tail)
                                if interfaces:
                                    self.__add_label_match(label_map, p_label, container.identifier)
                                    pre_matched.add(interfaces[-1].identifier)

                        labels.append([p_label, p_tail])

                    f_intfs = [pr for pr in function.param_interfaces if pr]
                    for pr in range(len(f_intfs)):
                        matched = set([label[0] for label in labels
                                       if label[0].name in label_map['matched labels'] and
                                       f_intfs[pr].identifier in label_map['matched labels'][label[0].name]]) & \
                                  set([label[0] for label in labels])
                        if len(matched) == 0 and f_intfs[pr].identifier not in pre_matched:
                            if len(labels) == len(f_intfs):
                                self.__add_label_match(label_map, labels[pr][0], f_intfs[pr].identifier)
                            else:
                                unmatched = [label[0] for label in labels
                                             if label[0].name not in label_map['matched labels'] and len(label[1]) == 0]
                                if len(unmatched) > 0:
                                    self.__add_label_match(label_map, unmatched[0], f_intfs[pr].identifier)
                                else:
                                    rsrs = [label[0] for label in labels if label[0].resource]
                                    if len(rsrs) > 0:
                                        self.__add_label_match(label_map, rsrs[-1], f_intfs[pr].identifier)

            # After containers are matched try to match rest callbacks from category
            matched_containers = [cn for cn in process.containers if cn.name in label_map["matched labels"]]
            unmatched_callbacks = [cl for cl in process.callbacks if cl.name not in label_map["matched labels"]]
            if len(matched_containers) > 0 and len(unmatched_callbacks) > 0:
                for callback in unmatched_callbacks:
                    for container in matched_containers:
                        for container_intf in [analysis.interfaces[intf] for intf in
                                               sorted(label_map["matched labels"][container.name])]:
                            for f_intf in [intf for intf in container_intf.field_interfaces.values()
                                           if type(intf) is Callback and not intf.called and
                                           intf.identifier not in label_map['matched callbacks']]:
                                self.__add_label_match(label_map, callback, f_intf.identifier)
            if len(unmatched_callbacks) > 0:
                for callback in unmatched_callbacks:
                    for intf in [intf for intf in analysis.callbacks(category)
                                 if not intf.called and intf.identifier not in label_map['matched callbacks']]:
                        self.__add_label_match(label_map, callback, intf.identifier)

            # Discard unmatched labels
            label_map["unmatched labels"] = [label for label in sorted(process.labels.keys())
                                             if label not in label_map["matched labels"] and
                                             len(process.labels[label].interfaces) == 0 and not
                                             process.labels[label].prior_signature]

            # Discard unmatched callbacks
            label_map["unmatched callbacks"] = []
            label_map["matched callbacks"] = []

            # Check which callbacks are matched totally
            for action in process.calls:
                label, tail = process.extract_label_with_tail(action.callback)
                if label.callback and label.name not in label_map["matched labels"] \
                        and action.callback not in label_map["unmatched callbacks"]:
                    label_map["unmatched callbacks"].append(action.callback)
                elif label.callback and label.name in label_map["matched labels"]:
                    callbacks = sorted(label_map["matched labels"][label.name])

                    for callback in callbacks:
                        if callback not in label_map["matched callbacks"]:
                            label_map["matched callbacks"].append(callback)
                        if action.callback not in label_map["matched calls"]:
                            label_map["matched calls"].append(action.callback)
                elif label.container and tail and label.name not in label_map["matched labels"] and \
                        action.callback not in label_map["unmatched callbacks"]:
                    label_map["unmatched callbacks"].append(action.callback)
                elif label.container and tail and label.name in label_map["matched labels"]:
                    for intf in sorted(label_map["matched labels"][label.name]):
                        intfs = self.__resolve_interface(analysis, analysis.interfaces[intf], tail, process, action)

                        if not intfs and action.callback not in label_map["unmatched callbacks"]:
                            label_map["unmatched callbacks"].append(action.callback)
                        elif intfs and action.callback not in label_map["matched callbacks"]:
                            label_map["matched callbacks"].append(intfs[-1].identifier)
                            if action.callback not in label_map["matched calls"]:
                                label_map["matched calls"].append(action.callback)

            # Discard uncalled callbacks and recalculate it
            label_map["uncalled callbacks"] = [cb.identifier for cb in analysis.callbacks(category)
                                               if cb.identifier not in label_map["matched callbacks"]]

            if len(label_map["matched callbacks"]) + len(label_map["matched labels"]) - old_size > 0:
                success_on_iteration = True

        # Discard unmatched callbacks
        for action in process.calls:
            label, tail = process.extract_label_with_tail(action.callback)
            if label.container and tail and label.name in label_map["matched labels"]:
                for intf in sorted(label_map["matched labels"][label.name]):
                    intfs = self.__resolve_interface(analysis, analysis.interfaces[intf], tail, process, action)
                    if intfs:
                        # Discard general callbacks match
                        for callback_label in [process.labels[name].name for name in sorted(process.labels.keys())
                                               if process.labels[name].callback and
                                               process.labels[name].name in label_map["matched labels"]]:
                            if intfs[-1].identifier in label_map["matched labels"][callback_label]:
                                label_map["matched labels"][callback_label].remove(intfs[-1].identifier)

        self.logger.info("Matched labels and interfaces:")
        self.logger.info("Number of native interfaces: {}".format(label_map["native interfaces"]))
        self.logger.info("Matched labels:")
        for label in sorted(label_map["matched labels"].keys()):
            self.logger.info("{} --- {}".
                             format(label, str(label_map["matched labels"][label])))
        self.logger.info("Unmatched labels:")
        for label in sorted(label_map["unmatched labels"]):
            self.logger.info(label)
        self.logger.info("Matched callbacks:")
        for callback in sorted(label_map["matched callbacks"]):
            self.logger.info(callback)
        self.logger.info("Uncalled callbacks:")
        for callback in sorted(label_map["uncalled callbacks"]):
            self.logger.info(callback)

        return label_map

    def __establish_signal_peers(self, analysis, process):
        for candidate in [self.__abstr_event_processes[name] for name in sorted(self.__abstr_event_processes.keys())]:
            peers = process.get_available_peers(candidate)

            # Be sure that process have not been added yet
            peered_processes = set()
            for subprocess in [process.actions[name] for name in sorted(process.actions.keys())
                               if (type(process.actions[name]) is Receive or type(process.actions[name]) is Dispatch)
                               and len(process.actions[name].peers) > 0]:
                peered_processes.update([peer["process"] for peer in subprocess.peers
                                         if peer["process"].name == candidate.name])

            # Try to add process
            if peers and len(peered_processes) == 0:
                self.logger.debug("Establish signal references between process {} with category {} and process {} with "
                                  "category {}".
                                  format(process.name, process.category, candidate.name, candidate.category))
                new = self.__add_process(analysis, candidate, process.category, model=False, label_map=None,
                                         peer=process)
                if new and (len(new.unmatched_receives) > 0 or len(new.unmatched_dispatches) > 0):
                    self.__establish_signal_peers(analysis, new)

        if len(process.unmatched_receives) > 0:
            for receive in process.unmatched_receives:
                if receive.name == "register":
                    # todo: Be sure that match will wotk with factories to add instance_register
                    self.logger.info("Generate default registration for process {} with category {}".
                                     format(process.name, process.category))
                    success = self.__predict_dispatcher(analysis, process, receive)
                    if not success:
                        self.__add_default_dispatch(process, receive)
                elif receive.name in ["deregister"]:
                    # todo: Be sure that match will wotk with factories to add instance_deregister
                    self.logger.info("Generate default deregistration for process {} with category {}".
                                     format(process.name, process.category))
                    success = self.__predict_dispatcher(analysis, process, receive)
                    if not success:
                        self.__add_default_dispatch(process, receive)
                else:
                    self.logger.warning("Signal {} cannot be received by process {} with category {}, "
                                        "since nobody can send it".
                                        format(receive.name, process.name, process.category))
        for dispatch in process.unmatched_dispatches:
            self.logger.warning("Signal {} cannot be send by process {} with category {}, "
                                "since nobody can receive it".format(dispatch.name, process.name, process.category))

    def __add_default_dispatch(self, process, receive):
        # Change name
        new_subprocess_name = "{}_{}_{}".format(receive.name, process.name, process.identifier)
        rename_subprocess(process, receive.name, new_subprocess_name)

        # Deregister dispatch
        self.logger.debug("Generate copy of receive {} and make dispatch from it".format(receive.name))
        new_dispatch = Dispatch(receive.name)

        # Peer these subprocesses
        new_dispatch.peers.append(
            {
                "process": process,
                "subprocess": process.actions[new_dispatch.name]
            })
        process.actions[new_dispatch.name].peers.append(
            {
                "process": self.entry_process,
                "subprocess": new_dispatch
            })

        self.logger.debug("Add dispatch {} to process {}".format(new_dispatch.name, self.entry_process.name))
        self.entry_process.actions[new_dispatch.name] = new_dispatch

        # todo: implement it taking into account that each parameter may have sevaral implementations
        # todo: relevant to issue #6566
        # Add labels if necessary
        #for index in range(len(new_dispatch.parameters)):
        #    parameter = new_dispatch.parameters[index]
        #
        #    # Get label
        #    label, tail = process.extract_label_with_tail(parameter)
        #
        #    # Copy label to add to dispatcher process
        #    new_label_name = "{}_{}_{}".format(process.name, label.name, process.identifier)
        #    if new_label_name not in self.entry_process.labels:
        #        new_label = copy.deepcopy(label)
        #        new_label.name = new_label_name
        #        self.logger.debug("To process {} add new label {}".format(self.entry_process.name, new_label_name))
        #        self.entry_process.labels[new_label.name] = new_label
        #    else:
        #        self.logger.debug("Process {} already has label {}".format(self.entry_process.name, new_label_name))
        #
        #    # Replace parameter
        #    new_dispatch.parameters[index] = parameter.replace(label.name, new_label_name)
        new_dispatch.parameters = []
        receive.parameters = []

        # Replace condition
        # todo: do this according to parameters (relevant to issue #6566)
        new_dispatch.condition = None
        receive.condition = None

    def __predict_dispatcher(self, analysis, process, receive):
        self.logger.info("Looking for a suitable kernel functions to use tham as registration and deregistration "
                         "for category '{}'".format(process.category))
        # Determine parameters
        parameter_sets = []
        for access in receive.parameters:
            label, tail = process.extract_label_with_tail(access)
            suits = []
            if label:
                for intf in label.interfaces:
                    if label and tail != '':
                        parameter = self.__resolve_interface(analysis, intf, tail, process, receive)
                        if parameter:
                            suits.append(parameter)
                    elif label:
                        suits.append(analysis.interfaces[intf])
            else:
                return False

            parameter_sets.append(suits)

        # Get suitable functions
        suits = analysis.find_relevant_function(parameter_sets)

        # Filter functions
        deregisters = [m for m in suits if len([regex for regex in self.__functions_map["deregistration"]
                                          if regex.search(m["function"].identifier)]) > 0]
        if receive.name in ['deregister', 'instance_deregister']:
            suits = deregisters
        elif receive.name in ['register', 'instance_register']:
            suits = [m for m in suits if len([regex for regex in self.__functions_map["registration"]
                                              if regex.search(m["function"].identifier)]) > 0 and
                     m not in deregisters]
        else:
            raise ValueError('Unknown default signal name {}'.format(receive.name))

        # Generate models
        for match in suits:
            existing_process = [p for p in self.model_processes if p.name == match['function'].identifier]
            if len(existing_process) > 0:
                return False
            new = Process(match['function'].identifier)

            # Generate labels
            expressions = []
            for interface in match['parameters']:
                new_label = Label(interface.short_identifier)
                new_label.parameter = True
                position = match['function'].param_interfaces.index(interface)
                expressions.append(["%{}%".format(new_label.name), position])

                declaration = match['function'].declaration.parameters[position]
                new_label.set_declaration(interface.identifier, declaration)

                new.labels[new_label.name] = new_label

            # Generate actions
            assign = Condition('assign')
            assign.statements = ['{} = $arg{};'.format(exp, str(pos + 1)) for exp, pos in expressions]
            new.actions[assign.name] = assign

            # Generate Process
            dispatch = Dispatch(receive.name)
            dispatch.parameters = [exp for exp, pos in expressions]
            new.actions[dispatch.name] = dispatch

            # Add process to the rest ones
            if receive.name in ['deregister', 'instance_deregister'] or \
                    not match['function'].declaration.return_value or \
                    match['function'].declaration.return_value.identifier != 'int':
                new.process = "<{}>.[{}]".format(assign.name, dispatch.name)
            else:
                assign.condition = ['$res == 0']
                none = Condition('none')
                none.condition = ['$res != 0']
                new.actions[none.name] = none

                new.process = "<{}>.[{}] | <{}>".format(assign.name, dispatch.name, none.name)

            self.__add_process(analysis, new, model=True, peer=process)

        if len(suits) > 0:
            return True
        else:
            return False

    def __resolve_interface(self, analysis, interface, string, process=None, action=None):
        tail = string.split(".")
        # todo: get rid of leading dot and support arrays
        if len(tail) == 1:
            raise RuntimeError("Cannot resolve interface for access '{}'".format(string))
        else:
            tail = tail[1:]

        if issubclass(type(interface), Interface):
            matched = [interface]
        elif type(interface) is str and interface in analysis.interfaces:
            matched = [analysis.interfaces[interface]]
        elif type(interface) is str and interface not in analysis.interfaces:
            return None
        else:
            raise TypeError("Expect Interface object but not {}".format(str(type(interface))))

        # Be sure the first interface is a container
        if type(matched[-1]) is not Container and len(tail) > 0:
            return None

        # Collect interface list
        for index in range(len(tail)):
            field = tail[index]

            # Match using a container field name
            intf = [matched[-1].field_interfaces[name] for name in sorted(matched[-1].field_interfaces.keys())
                    if name == field]

            # Match using an identifier
            if len(intf) == 0:
                intf = [matched[-1].field_interfaces[name] for name in sorted(matched[-1].field_interfaces.keys())
                        if matched[-1].field_interfaces[name].short_identifier == field]

            # Math using an interface role
            if process and action and type(action) is Call and len(intf) == 0 and self.__roles_map and \
                    field in self.__roles_map:
                intf = [matched[-1].field_interfaces[name] for name in sorted(matched[-1].field_interfaces.keys())
                        if matched[-1].field_interfaces[name].short_identifier in self.__roles_map[field] and
                        type(matched[-1].field_interfaces[name]) is Callback]

                # Filter by retlabel
                if action.retlabel and len(intf) > 0:
                    ret_label, ret_tail = process.extract_label_with_tail(action.retlabel)
                    if ret_tail == '' and ret_label and len(ret_label.declarations) > 0:
                        intf = [i for i in intf if len([r for r in ret_label.declarations
                                                        if i.declaration.points.return_value.compare(r)]) > 0]
                    else:
                        intf = []

                # filter parameters
                if len(intf) > 0:
                    # Collect parameters with declarations
                    param_match = []
                    for parameter in action.parameters:
                        p_label, p_tail = process.extract_label_with_tail(parameter)

                        if p_tail == '' and p_label and len(p_label.declarations) > 0:
                            param_match.append(p_label.declarations)

                    # Match parameters
                    new_intf = []
                    for interface in intf:
                        suits = 0
                        for index in range(len((param_match))):
                            found = 0
                            for param in interface.declaration.points.parameters[index:]:
                                for option in param_match[index]:
                                    if option.compare(param):
                                        found += 1
                                        break
                                if found:
                                    break
                            if found:
                                suits += 1
                        if suits == len(param_match):
                            new_intf.append(interface)
                    intf = new_intf

            if len(intf) == 0:
                return None
            else:
                if index == (len(tail) - 1) or type(intf[-1]) is Container:
                    matched.append(intf[-1])
                else:
                    return None

        self.logger.debug("Resolve string '{}' as '{}'".format(string, str(matched)))
        return matched

    def __resolve_accesses(self, analysis):
        self.logger.info("Convert interfaces access by expressions on base of containers and their fields")
        for process in sorted(self.model_processes + self.event_processes + [self.entry_process],
                              key=lambda pr: pr.identifier):
            self.logger.debug("Analyze subprocesses of process {} with category {}".
                              format(process.name, process.category))

            # Get empty keys
            accesses = process.accesses()

            # Fill it out
            original_accesses = sorted(accesses.keys())
            for access in original_accesses:
                label, tail = process.extract_label_with_tail(access)

                if not label:
                    raise ValueError("Expect label in {} in access in process description".format(access, process.name))
                else:
                    if len(label.interfaces) > 0:
                        for interface in label.interfaces:
                            new = Access(access)
                            new.label = label

                            # Add label access if necessary
                            label_access = "%{}%".format(label.name)
                            if label_access not in original_accesses:
                                # Add also label itself
                                laccess = Access(label_access)
                                laccess.label = label
                                laccess.interface = analysis.interfaces[interface]
                                laccess.list_interface = [analysis.interfaces[interface]]
                                laccess.list_access = [label.name]

                                if laccess.expression not in accesses:
                                    accesses[laccess.expression] = [laccess]
                                elif laccess.interface not in [a.interface for a in accesses[laccess.expression]]:
                                    accesses[laccess.expression].append(laccess)

                            # Calculate interfaces for tail
                            if len(tail) > 0:
                                callback_actions = [a for a in process.calls if a.callback == access]
                                options = []
                                if len(callback_actions) > 0:
                                    options = []
                                    for action in callback_actions:
                                        options.append(self.__resolve_interface(analysis, interface, tail, process,
                                                                                action))
                                else:
                                    options.append(self.__resolve_interface(analysis, interface, tail))

                                for intfs in (o for o in options if type(o) is list and len(o) > 0):
                                    list_access = []
                                    for index in range(len(intfs)):
                                        if index == 0:
                                            list_access.append(label.name)
                                        else:
                                            field = list(intfs[index - 1].field_interfaces.keys())\
                                                [list(intfs[index - 1].field_interfaces.values()).index(intfs[index])]
                                            list_access.append(field)
                                    new.interface = intfs[-1]
                                    new.list_access = list_access
                                    new.list_interface = intfs
                            else:
                                new.interface = analysis.interfaces[interface]
                                new.list_access = [label.name]
                                new.list_interface = [analysis.interfaces[interface]]

                            # Complete list accesses if possible
                            if new.interface:
                                new_tail = [new.interface]
                                to_process = [new.interface]
                                while len(to_process) > 0:
                                    interface = to_process.pop()
                                    category = new.interface.category

                                    for container in analysis.containers(category):
                                        if container.weak_contains(interface) and container not in new_tail:
                                            new_tail.append(container)
                                            to_process.append(container)
                                            break
                                new_tail.reverse()
                                new.complete_list_interface = new_tail

                            accesses[access].append(new)
                    else:
                        new = Access(access)
                        new.label = label
                        new.list_access = [label.name]
                        accesses[access].append(new)

                        # Add also label itself
                        label_access = "%{}%".format(label.name)
                        if label_access not in original_accesses:
                            laccess = Access(label_access)
                            laccess.label = label
                            laccess.list_access = [label.name]
                            accesses[laccess.expression] = [laccess]

            # Save back updates collection of accesses
            process.accesses(accesses)

__author__ = 'Ilja Zakharov <ilja.zakharov@ispras.ru>'