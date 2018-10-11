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

import ujson
import zipfile


from core.vog.abstractions.strategies import Abstract
import core.utils


class Coverage(Abstract):
    """
    This strategy gets information about coverage of fragments and searches for suitable fragments to add to cover
    functions exported by target ones.
    """

    def __init__(self, logger, conf, desc, deps):
        super().__init__(logger, conf, desc, deps)
        self.archive = self.desc.get('coverage archive')
        self._black_list = set(self.desc.get('ignore fragments', set()))
        self._white_list = set(self.desc.get('prefer fragments', set()))

        # Get archive
        archive = core.utils.find_file_or_dir(self.logger, self.conf['main working directory'], self.archive)

        # Extract/fetch file
        with zipfile.ZipFile(archive) as z:
            with z.open('coverage.json') as zf:
                coverage = ujson.load(zf)

        # Extract information on functions
        self._func_coverage = coverage.get('functions statistics')
        if not self._func_coverage or not self._func_coverage.get('statistics'):
            raise ValueError("There is no statictics about functions in the given coverage archive")
        self._func_coverage = {p.replace('source files/', ''): v
                               for p, v in self._func_coverage.get('statistics').items()}
        self._func_coverage.pop('overall')

    def _aggregate(self):
        """
        Just return target fragments as aggregations consisting of a single fragment.

        :return: Generator that retursn Aggregation objects.
        """
        # Get target fragments
        for fragment in self.deps.target_fragments:
            # Search for export functions
            ranking = dict()
            function_map = dict()
            stats = self.deps.find_files_that_use_functions(fragment.export_functions)
            for func in stats:
                for rel in stats[func]:
                    ranking.setdefault(rel.name, 0)
                    ranking[rel.name] += 1
                    function_map.setdefault(func, set())
                    function_map[func].add(rel)

            # Use a greedy algorythm. Go from functions that most rarely used and add fragments that most oftenly used
            # Turn into account white and black lists
            added = set()
            for func in (f for f in sorted(function_map.keys(), key=lambda x: len(function_map[x]))
                         if len(function_map[f])):
                if function_map[func].intersection(added):
                    # Already added
                    continue
                else:
                    possible = {f.name for f in function_map[func]}.intersection(self._white_list)
                    if not possible:
                        # Get rest
                        possible = {f.name for f in function_map[func]}.difference(self._black_list)
                    if possible:
                        added.add(sorted((f for f in function_map[func] if f.name in possible),
                                         key=lambda x: ranking[x.name], reverse=True)[0])

            # Now generate pairs
            for frag in added:
                name = "{}:{}".format(fragment.name, frag.name)
                self.add_group(name, {fragment, frag})
            self.add_group(fragment.name, {fragment})

        # Free data
        self._func_coverage = None