# Copyright (c) Weasyl LLC
# See COPYING for details.

import argparse
import datetime
import os.path
import re

import elastirc

from whoosh import index


line_pattern = re.compile(
    r'(?P<time>[0-9:]{8}) (?P<formatted>'
      r'\(-\) (?P<actor>[^ ]+?) '
        r'(?P<action>joined|parted|quit'
        r'|was kicked by (?P<kicker>[^ ]+?)'
        r'|changed nick from (?P<oldName>[^ ]+?)'
        r'|changed topic to (?P<topic>.*)'
        r'|set mode .+)'
        r'(?: \((?P<reason>.*)\))?'
      r'|<(?P<message_actor>[^>]+?)> (?P<message>.*)'
      r'|\* (?P<emote_actor>[^ ]+?) (?P<emote>.*)'
    r')'
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--create-index', default=False, action='store_true')
    parser.add_argument('index')
    parser.add_argument('infiles', nargs='*', type=argparse.FileType('r'))
    args = parser.parse_args()

    if args.create_index:
        if not os.path.exists(args.index):
            os.makedirs(args.index)
        ix = index.create_in(args.index, elastirc.whooshSchema)
    else:
        ix = index.open_dir(args.index)

    writer = ix.writer()

    for infile in args.infiles:
        basename = os.path.basename(infile.name)
        print 'indexing', basename
        channel, _, date = basename.rpartition('.')
        channel = channel.decode('utf-8')
        for line in infile:
            line = line.decode('utf-8')
            groups = line_pattern.match(line).groupdict()
            if groups['message_actor']:
                doc = {'actor': groups['message_actor'], 'message': groups['message']}
            elif groups['emote_actor']:
                doc = {'actor': groups['emote_actor'], 'message': groups['emote']}
            else:
                doc = {}
                for key in ['actor', 'kicker', 'oldName', 'topic', 'reason']:
                    if groups[key]:
                        doc[key] = groups[key]
            doc['formatted'] = groups['formatted']
            doc['channel'] = channel
            doc['receivedAt'] = datetime.datetime.strptime(
                '%sT%s' % (date, groups['time']), '%Y-%m-%dT%H:%M:%S')
            writer.add_document(**doc)

    writer.commit()


main()
