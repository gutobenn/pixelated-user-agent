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

from pixelated.support.encrypted_file_storage import EncryptedFileStorage

import os
from pixelated.adapter.model.status import Status
from pixelated.support.functional import flatten
from whoosh.index import FileIndex
from whoosh.fields import *
from whoosh.qparser import QueryParser
from whoosh.qparser import MultifieldParser
from whoosh.query import Term
from whoosh import sorting
from pixelated.support.functional import unique
from pixelated.support.date import milliseconds
from threading import Lock
import traceback


class SearchEngine(object):
    DEFAULT_INDEX_HOME = os.path.join(os.environ['HOME'], '.leap')
    DEFAULT_TAGS = ['inbox', 'sent', 'drafts', 'trash']

    def __init__(self, soledad_querier, agent_home=DEFAULT_INDEX_HOME):
        self.soledad_querier = soledad_querier
        self.index_folder = os.path.join(agent_home, 'search_index')
        if not os.path.exists(self.index_folder):
            os.makedirs(self.index_folder)
        self._index = self._create_index()
        self._write_lock = Lock()

    def _add_to_tags(self, tags, group, skip_default_tags, count_type, query=None):
        query_matcher = re.compile(".*%s.*" % query.lower()) if query else re.compile(".*")

        for tag, count in group.iteritems():

            if skip_default_tags and tag in self.DEFAULT_TAGS or not query_matcher.match(tag):
                continue

            if not tags.get(tag):
                tags[tag] = {'ident': tag, 'name': tag, 'default': False, 'counts': {'total': 0, 'read': 0},
                             'mails': []}
            tags[tag]['counts'][count_type] += count

    def _search_tag_groups(self, is_filtering_tags):
        seen = None
        query_parser = QueryParser('tag', self._index.schema)
        options = {'limit': None, 'groupedby': sorting.FieldFacet('tag', allow_overlap=True), 'maptype': sorting.Count}

        with self._index.searcher() as searcher:
            total = searcher.search(query_parser.parse('*'), **options).groups()
            if not is_filtering_tags:
                seen = searcher.search(query_parser.parse("* AND flags:%s" % Status.SEEN), **options).groups()
        return seen, total

    def _init_tags_defaults(self):
        tags = {}
        for default_tag in self.DEFAULT_TAGS:
            tags[default_tag] = {
                'ident': default_tag,
                'name': default_tag,
                'default': True,
                'counts': {
                    'total': 0,
                    'read': 0
                },
                'mails': []
            }
        return tags

    def _build_tags(self, seen, total, skip_default_tags, query):
        tags = {}
        if not skip_default_tags:
            tags = self._init_tags_defaults()
        self._add_to_tags(tags, total, skip_default_tags, count_type='total', query=query)
        if seen:
            self._add_to_tags(tags, seen, skip_default_tags, count_type='read')
        return tags.values()

    def tags(self, query, skip_default_tags):
        is_filtering_tags = True if query else False
        seen, total = self._search_tag_groups(is_filtering_tags=is_filtering_tags)
        return self._build_tags(seen, total, skip_default_tags, query)

    def _mail_schema(self):
        return Schema(
            ident=ID(stored=True, unique=True),
            sender=ID(stored=False),
            to=KEYWORD(stored=False, commas=True),
            cc=KEYWORD(stored=False, commas=True),
            bcc=KEYWORD(stored=False, commas=True),
            subject=TEXT(stored=False),
            date=NUMERIC(stored=False, sortable=True, bits=64, signed=False),
            body=TEXT(stored=False),
            tag=KEYWORD(stored=True, commas=True),
            flags=KEYWORD(stored=True, commas=True),
            raw=TEXT(stored=False))

    def _create_index(self):
        masterkey = self.soledad_querier.get_index_masterkey()
        storage = EncryptedFileStorage(self.index_folder, masterkey)
        return FileIndex.create(storage, self._mail_schema(), indexname='mails')

    def index_mail(self, mail):
        with self._index.writer() as writer:
            self._index_mail(writer, mail)

    def _index_mail(self, writer, mail):
        mdict = mail.as_dict()
        header = mdict['header']
        tags = mdict.get('tags', [])
        tags.append(mail.mailbox_name.lower())
        index_data = {
            'sender': unicode(header.get('from', '')),
            'subject': unicode(header.get('subject', '')),
            'date': milliseconds(header.get('date', '')),
            'to': u','.join(header.get('to', [''])),
            'cc': u','.join(header.get('cc', [''])),
            'bcc': u','.join(header.get('bcc', [''])),
            'tag': u','.join(unique(tags)),
            'body': unicode(mdict['textPlainBody']),
            'ident': unicode(mdict['ident']),
            'flags': unicode(','.join(unique(mail.flags))),
            'raw': unicode(mail.raw)
        }

        writer.update_document(**index_data)

    def index_mails(self, mails, callback=None):
        try:
            with self._write_lock:
                with self._index.writer() as writer:
                    for mail in mails:
                        self._index_mail(writer, mail)
            if callback:
                callback()
        except Exception, e:
            traceback.print_exc(e)
            raise

    def _search_with_options(self, options, query):
        with self._index.searcher() as searcher:
            query = QueryParser('raw', self._index.schema).parse(query)
            results = searcher.search(query, **options)
        return results

    def search(self, query, window=25, page=1, all_mails=False):
        query = self.prepare_query(query)
        return self._search_all_mails(query) if all_mails else self._paginated_search_mails(query, window, page)

    def _search_all_mails(self, query):
        with self._index.searcher() as searcher:
            sorting_facet = sorting.FieldFacet('date', reverse=True)
            results = searcher.search(query, sortedby=sorting_facet, reverse=True, limit=None)
            return unique([mail['ident'] for mail in results])

    def _paginated_search_mails(self, query, window, page):
        page = int(page) if page is not None and int(page) > 1 else 1
        window = int(window) if window is not None else 25

        with self._index.searcher() as searcher:
            tags_facet = sorting.FieldFacet('tag', allow_overlap=True, maptype=sorting.Count)
            sorting_facet = sorting.FieldFacet('date', reverse=True)
            results = searcher.search_page(query, page, pagelen=window, groupedby=tags_facet, sortedby=sorting_facet)
            return unique([mail['ident'] for mail in results]), sum(results.results.groups().values())

    def prepare_query(self, query):
        query = (
            query
            .replace('\"', '')
            .replace('-in:', 'AND NOT tag:')
            .replace('in:all', '*')
        )
        return MultifieldParser(['raw', 'body'], self._index.schema).parse(query)

    def remove_from_index(self, mail_id):
        with self._write_lock:
            writer = self._index.writer()
            try:
                writer.delete_by_term('ident', mail_id)
            finally:
                writer.commit()

    def contacts(self, query):
        restrict_q = Term("tag", "drafts") | Term("tag", "trash")

        if query:
            to = QueryParser('to', self._index.schema)
            cc = QueryParser('cc', self._index.schema)
            bcc = QueryParser('bcc', self._index.schema)
            with self._index.searcher() as searcher:
                to = searcher.search(to.parse("*%s*" % query), limit=None, mask=restrict_q,
                                     groupedby=sorting.FieldFacet('to', allow_overlap=True)).groups()
                cc = searcher.search(cc.parse("*%s*" % query), limit=None, mask=restrict_q,
                                     groupedby=sorting.FieldFacet('cc', allow_overlap=True)).groups()
                bcc = searcher.search(bcc.parse("*%s*" % query), limit=None, mask=restrict_q,
                                      groupedby=sorting.FieldFacet('bcc', allow_overlap=True)).groups()
                return flatten([to, cc, bcc])

        return []
