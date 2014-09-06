#
# Copyright (c) 2014 ThoughtWorks, Inc.
#
# Pixelated is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Pixelated is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Pixelated. If not, see <http://www.gnu.org/licenses/>.

import dbm
import atexit
from pixelated.adapter.tag import Tag


class TagIndex:
    """
    Manages an index for mail's tags using a file storage.
    """

    def __init__(self, filename):
        self.db = dbm.open(filename, 'c')
        atexit.register(self.close_db)

    def set(self, tag):
        self.db[tag.name] = tag.as_json_string()

    def get(self, tag_name):
        if tag_name in self.db:
            return Tag.from_json_string(self.db.get(tag_name))
        else:
            return None

    def values(self):
        return set(self.get(key) for key in self.db.keys())

    def close_db(self):
        self.db.close()