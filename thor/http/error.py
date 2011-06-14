#!/usr/bin/env python

"""
Thor HTTP Errors
"""

__author__ = "Mark Nottingham <mnot@mnot.net>"
__copyright__ = """\
Copyright (c) 2008-2010 Mark Nottingham

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

class HttpError(Exception):
    desc = "Unknown Error"
    server_status = None # status this produces when it occurs in a server
    server_recoverable = False # whether a server can recover the connection

    def __init__(self, detail=None):
        Exception.__init__(self)
        self.detail = detail

# General parsing errors

class ChunkError(HttpError):
    desc = "Chunked encoding error"

class ContentLengthError(HttpError):
    desc = "Duplicate or Malformed Content-Length header."
    server_status = ("400", "Bad Request")

class BodyForbiddenError(HttpError):
    desc = "This message does not allow a body",

class HttpVersionError(HttpError):
    desc = "Unrecognised HTTP version"
    server_status = ("505", "HTTP Version Not Supported")

class ReadTimeoutError(HttpError):
    desc = "Read Timeout"

class TransferCodeError(HttpError):
    desc = "Unknown request transfer coding"
    server_status = ("501", "Not Implemented")

class HeaderSpaceError(HttpError):
    desc = "Whitespace at the end of a header field-name"
    server_status = ("400", "Bad Request")
    
class TopLineSpaceError(HttpError):
    desc = "Whitespace after top line, before first header"
    server_status = ("400", "Bad Request")

class TooManyMsgsError(HttpError):
    desc = "Too many messages to parse"
    server_status = ("400", "Bad Request")

# client-specific errors

class UrlError(HttpError):
    desc = "Unsupported or invalid URI"
    server_status = ("400", "Bad Request")

class LengthRequiredError(HttpError):
    desc = "Content-Length required"
    server_status = ("411", "Length Required")

class ConnectError(HttpError):
    desc = "Connection closed"
    server_status = ("504", "Gateway Timeout")

# server-specific errors

class HostRequiredError(HttpError):
    desc = "Host header required"
    server_recoverable = True