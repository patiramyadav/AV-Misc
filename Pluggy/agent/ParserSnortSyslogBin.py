#
# License:
#
#    Copyright (c) 2003-2006 ossim.net
#    Copyright (c) 2007-2011 AlienVault
#    All rights reserved.
#
#    This package is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; version 2 dated June, 1991.
#    You may not use, modify or distribute this program under any other version
#    of the GNU General Public License.
#
#    This package is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this package; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston,
#    MA  02110-1301  USA
#
#
# On Debian GNU/Linux systems, the complete text of the GNU General
# Public License can be found in `/usr/share/common-licenses/GPL-2'.
#
# Otherwise you can read it here: http://www.gnu.org/licenses/gpl-2.0.txt
#

#
# GLOBAL IMPORTS
#
import os, sys, time, re, socket
from time import sleep
import pdb
import pyinotify #deb package python-pyinotify
# python-pyinotify version 0.7.1-1
from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent
from threading import Lock
#
# LOCAL IMPORTS
#
from Profiler import TimeProfiler
from Detector import Detector
from Event import Snort
from Logger import Logger
from TailFollow import TailFollow
from TailFollowBookmark import TailFollowBookmark
import glob
import base64
import zlib
from binascii import hexlify
from PacketUtils import UDPPacket, IPPacket, RawPacket, TCPPacket, getprotobynumber
logger = Logger.logger

class SnortSyslogBinRuleMatch:

    NEWLINE = "\\n"

    def __init__(self, name, rule, plugin):
        logger.debug("Adding rule (%s).." % (name))

        self.rule = rule
        self.name = name
        self.plugin = plugin
        self.encode = plugin.get('config','encoding')
        # store {precheck:, regexp: , pattern: , result: } hashes
        self.lines = []


        # precheck
        # The precheck directive allows for a particular string
        # to be searched as a prerequisite, before conducting
        # an expensive regex.search()
        #
        try:
            precheck = self.rule["precheck"]
        except:
            precheck = ""

        regexp = self.rule["regexp"]
        regex_flags = re.IGNORECASE | re.UNICODE
        for r in regexp.split(SnortSyslogBinRuleMatch.NEWLINE):
            try:
                self.lines.append({
                    "precheck": precheck,
                    "regexp": r,
                    "pattern": re.compile(r, regex_flags),
                    "result": None})
            except Exception, e:
                logger.error("Error reading rule [%s]: %s" % (self.name, e))

        self.nlines = regexp.count(SnortSyslogBinRuleMatch.NEWLINE) + 1
        self.line_count = 1
        self.matched = False
        self.log = ""
        self.groups = {}


        # in order to eliminate unnecessary calls to the expensive re.findall(),
        # perform assessments on the _replace_* functions of the Conf class to
        # determine which are necessary.
        self._replace_assessment = {}

        for key, value in self.rule.iteritems():
            if key != "regexp":
                self._replace_assessment[key] = self.plugin.replace_value_assess(value)

    def resetRule(self):
        self.line_count = 1
        self.log = ''
        self.matched = False
        for line in self.lines:
            line['result'] = None
    def feed(self, line):

        self.matched = False
        self.groups = {}

        line_index = self.line_count - 1
        if len(self.lines) > line_index:

            if line.find(self.lines[line_index]["precheck"]) != -1:
                self.lines[line_index]["result"] = self.lines[line_index]["pattern"].search(line)

                # (logs for multiline rules)
                # Fill the log attribute with all its lines,
                # not only with the last one matched
                if line_index == 0:
                    self.log = ""

                self.log += line.rstrip() + " "

                if self.line_count == self.nlines:
                    if self.lines[line_index]["result"] is not None: # matched!
                        self.matched = True
                        self.line_count = 1
                    else:
                        #Line not matched, reset rule.
                        self.log = ''
                        self.matched = False
                        self.line_count = 1
                else:
                    if self.lines[line_index]["result"] is not None: # matched!
                        self.line_count += 1

                    else:
                        self.line_count = 1

        else:
            logger.error("There was an error loading rule [%s]" % (self.name))


    def match(self):
        if self.matched:
            self.group()

        return self.matched


    # convert the list of pattern objects to a dictionary
    def group(self):

        self.groups = {}
        count = 1

        if self.matched:
            for line in self.lines:

                # group by index ()
                groups = line["result"].groups()
                for group in groups:
                    if group is None:
                        group = '' # convert to '' better than 'None'
                    value = str(group.encode('utf-8'))
                    self.groups.update({str(count): value})
                    count += 1

                # group by name (?P<name-of-group>)
                groups = line["result"].groupdict()
                for key, group in groups.iteritems():
                    if group is None:
                        group = '' # convert to '' better than 'None'
                    value = ''
                    value = str(group.encode('utf-8'))
                    self.groups.update({str(key): value})


    def generate_event(self):

        if not self.rule.has_key('event_type'):
            logger.error("Event has no type, check plugin configuration!")
            return None
        # create the Snort event
        event = Snort()
        event["event_type"] = Snort.EVENT_TYPE

        if event['interface'] is None:
            event["interface"] = self.plugin.get("config", "interface")
        if self.groups.has_key('binpacket'):
            b64_packet = self.groups['binpacket']
            payload  = base64.b64decode(b64_packet)
            payload_len = len(payload)
            ip = IPPacket(payload)
            logger.info("DATE: %s" % self.groups['date'])
            st = """type="detector" date="%s" fdate="%s" """ + \
             """snort_gid="%u" snort_sid="%s" snort_rev="%u" """ + \
             """snort_classification="%u" snort_priority="%u" """ 
            lt = time.localtime(time.time())
            st = st % (int(time.mktime(lt)), time.strftime("%Y-%m-%d %H:%M:%S", lt), \
                1, self.groups['sid'], 0, \
                1, 3)
            st += """ packet_type="ip" """
            st += str(ip)
            data = zlib.compress(st)
#            event['userdata1'] = hexlify(data) 
#            event['userdata2'] = str(len(str(ip)))
        else:
            logger.warning("SnortSyslogBin without binpacket group!")
            return None
        (event["unziplen"], event["gzipdata"]) = len(st),hexlify(data)

        if event['plugin_id'] is None:
            event['plugin_id'] = self.plugin.get("config", "plugin_id")

        if event['type'] is None:
            event['type'] = self.plugin.get("config", "type")
        return event

class FileEventHandler(pyinotify.ProcessEvent):
    
    def __init__(self, ptrTail):
        self.__ptrTail = ptrTail

    def process_IN_CREATE(self, event):
        """
        Version 0.8.x:
        logger.info("File: %s has been created!" % event.pathname)
        self.__ptrTail(event.pathname)
        """
        pathname = os.path.join(event.path, event.name)
        logger.info("File: %s has been created!" % pathname)
        self.__ptrTail(pathname)
class ParserSnortSyslogBin(Detector):
    '''
        Snort logs parser for syslog and binary data:
        example: Feb 14 13:15:43 HOST snort: [1:2100384:6] GPL ICMP_INFO PING [Classification: Misc activity] [Priority: 3]: {ICMP} 10.0.0.1 -> 10.0.0.5 [RQAAHA3QAAD/ATrTCpY/EQqWHwEIAPBgAAEHngAAAAAAAAAAAAAAAAAAAAAAAA==]
    '''

    def __init__(self, conf, plugin):
        self._conf = conf        # config.cfg info
        self._plugin = plugin    # plugins/X.cfg info
        self.rules = []          # list of RuleMatch objects
        Detector.__init__(self, conf, plugin)
        self.__tailLock = Lock()
        self.stop_processing = False
        self.__locations = []
        self.__watchdog = pyinotify.WatchManager()
        self.__notifier = None#pyinotify.ThreadedNotifier(self.__watchdog, FileEventHandler())
        self.__startNotifier = False
        self.__tails = []
        self.__monitorLocations = []
        self.__bookmark_dir = ""
    def check_file_path(self, location):
        can_read = True
        if self._plugin.has_option("config", "create_file"):
            create_file = self._plugin.getboolean("config", "create_file")
        else:
            create_file = False

        if not os.path.exists(location) and create_file:
            if not os.path.exists(os.path.dirname(location)):
                logger.warning("Creating directory %s.." % \
                    (os.path.dirname(location)))
                os.makedirs(os.path.dirname(location), 0755)

            logger.warning("Can not read from file %s, no such file. " % \
                (location) + "Creating it..")
            fd = open(location, 'w')
            fd.close()
        
        # open file
        fd = None
        try:
            #check if file exist.
            if os.path.isfile(location):
                fd = open(location, 'r')
            else:
                logger.warning("File: %s does not exist!" % location)
                can_read = False

        except IOError, e:
            logger.error("Can not read from file %s: %s" % (location, e))
            can_read = False
            #sys.exit()
        if fd is not None:
            fd.close()
        return can_read


    def stop(self):
        logger.debug("Scheduling stop of ParserLog.")
        self.stop_processing = True
        if self.__startNotifier:
            self.__notifier.stop()
        try:
            self.join()
        except RuntimeError:
            logger.warning("Stopping thread that likely hasn't started.")


    def addTail(self, newlocation):
        self.__tailLock.acquire()
        for  location in self.__monitorLocations:
            if location == newlocation:
                self.__tails.append(TailFollowBookmark(location, 1, self.__bookmark_dir, self._plugin.getboolean("config", "unicode_support")))
                self.__monitorLocations.remove(location)
                break
        self.__tailLock.release()


    def resetAllrules(self):
        for rule in self.rules:
            rule.resetRule()

    def process(self):
        self.__notifier = pyinotify.ThreadedNotifier(self.__watchdog, FileEventHandler(self.addTail))
        try:
            self.__bookmark_dir = self._plugin.get("config", "bookmark_dir")
        except:
            self.__bookmark_dir = ""
        mask = EventsCodes.IN_CREATE #pyinotify.IN_CREATE
        
        #check if the plugin has rlocation
        locations = []
        rlocationvalue = self._plugin.get("config", "rlocation")
        if rlocationvalue != "":
            files = glob.glob(rlocationvalue)
            for f in files:
                logger.debug("Adding location :%s" % f)
                locations.append(f)
        else:
            locations = self._plugin.get("config", "location")
            locations = locations.split(',')
        self.__notifier.start()
        # first check if file exists
        for location in locations:
            if self.check_file_path(location):
                self.__locations.append(location)
            else:
                self.__monitorLocations.append(location)
                dir = os.path.dirname(location)
#                if not self.__watchdog.watches.has_key(dir): #--version python-pyinotify 0.8.9
                self.__watchdog.add_watch(dir, mask, rec=True)

        # compile the list of regexp
        unsorted_rules = self._plugin.rules()
        keys = unsorted_rules.keys()
        keys.sort()
        for key in keys:
            item = unsorted_rules[key]
            self.rules.append(SnortSyslogBinRuleMatch(key, item, self._plugin))

        # Move to the end of file
        # fd.seek(0, 2)

        for location in self.__locations:
            self.__tails.append(TailFollowBookmark(location, 1, self.__bookmark_dir, self._plugin.get("config", "encoding")))

        while not self.stop_processing:
            # is plugin enabled?
            if not self._plugin.getboolean("config", "enable"):

                # wait until plugin is enabled
                while not self._plugin.getboolean("config", "enable"):
                    time.sleep(1)

                # plugin is now enabled, skip events generated on
                # 'disable' state, so move to the end of file

            self._thresholding()

            for tail in self.__tails:
                # stop processing tails if requested
                if self.stop_processing:
                    break

                for line in tail:
                    matches = 0
                    rules = 0

                    # stop processing lines if requested
                    if self.stop_processing:
                        break
                    rule_matched = False
                    for rule in self.rules:
                        rules += 1
                        rule.feed(line)

                        if rule.match() and not rule_matched:
                            matches += 1
                            event = rule.generate_event()
                            self.resetAllrules()
                            # send the event as appropriate
                            if event is not None:
                                self.send_message(event)

                            # one rule matched, no need to check more
                            rule_matched = True
                            break
            #Added small sleep to avoid the excessive cpu usage
            time.sleep(0.01)

        for tail in self.__tails:
            tail.close()
        logger.debug("Processing completed.")
# vim:ts=4 sts=4 tw=79 expandtab: