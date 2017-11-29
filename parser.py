import logging
import six
import time

from errno import ENOENT
from stat import S_IFDIR, S_IFREG
from datetime import datetime
from bs4 import BeautifulSoup
from collections import defaultdict
from fuse import FuseOSError, EIO


class Directory:
    def __init__(self, root, path, session):
        self.root = root
        self.path = path
        self.session = session
        self.log = logging.getLogger("Directory")
        self.log.debug(u"[INIT] Loading directory {}/{}".format(root, path))

    def contents(self):
        """
        Give the contents of the directory
        :return: List of Entities that are in the directory
        :rtype: list
        """
        contents = [(".", True), ("..", True)]

        # Do a request, and run it through an HTML parser.
        response = self.session.get(u"{}/{}/".format(self.root, self.path))
        parsed = BeautifulSoup(response.text, 'html.parser')

        # Find all of the entity elements, remove the cruft
        for x in parsed.find_all("tr"):
            if x.td is not None and x.td.img['alt'] != "[PARENTDIR]":
                is_dir = x.td.img['alt'] == "[DIR]"
                contents.append((x.find_all('td')[1].a.string.strip("/"), is_dir))

        return contents


class File:
    def __init__(self, root, path, httpfs, session, dirmtime=False):
        self.root = root
        self.path = path
        self.session = session
        self.log = logging.getLogger("File")
        self.log.debug(u"[INIT] Loading file {}/{}".format(root, path))
        self.readbuffer = defaultdict(lambda: None)
        self.dirmtime = dirmtime

        # Determine if this is a directory
        parent_dir = "/".join(self.path.split("/")[:-1])
        filename = self.path.split("/")[-1]
        if parent_dir not in httpfs.readdir_cache.keys():
            httpfs.readdir_cache[parent_dir] = Directory(self.root, parent_dir, self.session).contents()

        dirs = [six.text_type(x[0]) for x in httpfs.readdir_cache[parent_dir] if x[1]]
        self.is_dir = (six.text_type(filename) in dirs) or six.text_type(filename) == six.text_type("")

        # Determine file size
        self.url = u"{}/{}{}".format(self.root, self.path, "/" if self.is_dir else "")
        self.r = self.session.head(self.url, allow_redirects=True)
        if self.r.status_code == 200:
            try:
                self.size = int(self.r.headers['Content-Length'])
            except KeyError:
                self.size = 0

            try:
                mtime_string = self.r.headers["Last-Modified"]
                self.mtime = time.mktime(datetime.strptime(mtime_string, "%a, %d %b %Y %H:%M:%S %Z").timetuple())
            except KeyError:
                self.mtime = None if self.is_dir and self.dirmtime else time.time()
            if self.mtime is None:
                # parse modified time from html if we can't get from header
                response = self.session.get(u"{}/{}/".format(self.root, parent_dir))
                parsed = BeautifulSoup(response.text, 'html.parser')
                for x in parsed.find_all("tr"):
                    if x.td is not None and x.td.img['alt'] != "[PARENTDIR]":
                        row_tds = x.find_all('td')
                        if filename in row_tds[1].a.string:
                            self.mtime = time.mktime(datetime.strptime(row_tds[2].string.strip(), "%Y-%m-%d %H:%M").timetuple())
        else:
            self.log.info(u"[INIT] Non-200 code while getting {}: {}".format(self.url, self.r.status_code))
            self.size = 0

    def read(self, length, offset):
        """
        Reads the file.
        :param length: The length to read
        :param offset: The offset to start at
        :return: The file's bytes
        """
        self.log.debug(u"[READ] Reading file {}/{}".format(self.root, self.path))
        url = u"{}/{}".format(self.root, self.path)

        # Calculate megabyte-section this offset/length is in
        mb_start = (offset // 1024) // 1024
        mb_end = ((offset + length) // 1024) // 1024
        offset_from_mb = (((offset // 1024) % 1024) * 1024) + (offset % 1024)
        self.log.debug(u"Calculated MB_Start {} MB_End {} Offset from MB: {}".format(mb_start, mb_end, offset_from_mb))
        if mb_start == mb_end:
            self.log.debug(u"Readbuffer filled for mb_start? {}".format(self.readbuffer[mb_start] is not None))
            if self.readbuffer[mb_start] is None:
                # Fill buffer for this MB
                bytesRange = u'{}-{}'.format(mb_start * 1024 * 1024, (mb_start * 1024 * 1024) + (1023 * 1024))
                self.log.debug(u"Fetching byte range {}".format(bytesRange))
                headers = {'range': 'bytes=' + bytesRange}
                r = self.session.get(url, headers=headers)
                if r.status_code == 200 or r.status_code == 206:
                    self.readbuffer[mb_start] = r.content
                    # noinspection PyTypeChecker
                    self.log.debug(u"Read {} bytes.".format(len(self.readbuffer[mb_start])))
                else:
                    self.log.info(u"[INIT] Non-200 code while getting {}: {}".format(url, r.status_code))
                    raise FuseOSError(EIO)

            self.log.debug(u"Returning indices {} to {}".format(offset_from_mb, offset_from_mb+length))
            return self.readbuffer[mb_start][offset_from_mb:offset_from_mb+length]
        else:
            self.log.debug(u"Offset/Length spanning multiple MB's. Fetching normally")
            # Spanning multiple MB's, just get it normally
            # Set range
            bytesRange = u'{}-{}'.format(offset, min(self.size, offset + length - 1))
            self.log.debug(u"Fetching byte range {}".format(bytesRange))
            headers = {'range': 'bytes=' + bytesRange}
            r = self.session.get(url, headers=headers)
            if self.r.status_code == 200 or r.status_code == 206:
                return r.content
            else:
                self.log.info(u"[INIT] Non-200 code while getting {}: {}".format(url, r.status_code))
                raise FuseOSError(EIO)

    def attributes(self):
        self.log.debug(u"[ATTR] Attributes of file {}/{}".format(self.root, self.path))

        if self.r.status_code != 200:
            raise FuseOSError(ENOENT)

        mode = (S_IFDIR | 0o777) if self.is_dir else (S_IFREG | 0o666)

        attrs = {
            'st_atime': self.mtime,
            'st_mode': mode,
            'st_mtime': self.mtime,
            'st_size': self.size,
        }

        if self.is_dir:
            attrs['st_nlink'] = 2

        return attrs
