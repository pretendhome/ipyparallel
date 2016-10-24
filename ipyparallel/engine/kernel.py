"""IPython kernel for parallel computing"""

import sys

from ipython_genutils.py3compat import cast_bytes, cast_unicode_py2, unicode_type, safe_unicode, string_types
from traitlets import Integer, Type

from ipykernel.ipkernel import IPythonKernel
from ipykernel.jsonutil import json_clean
from ipyparallel.serialize import serialize_object, unpack_apply_message
from ipyparallel.util import utcnow
from .datapub import ZMQDataPublisher


class IPythonParallelKernel(IPythonKernel):
    """Extend IPython kernel for parallel computing"""
    engine_id = Integer(-1)
    msg_types = getattr(IPythonKernel, 'msg_types', []) + ['apply_request']
    control_msg_types = getattr(IPythonKernel, 'control_msg_types', []) + ['abort_request', 'clear_request']
    _execute_sleep = 0
    data_pub_class = Type(ZMQDataPublisher)
    
    def _topic(self, topic):
        """prefixed topic for IOPub messages"""
        base = "engine.%s" % self.engine_id

        return cast_bytes("%s.%s" % (base, topic))

    def __init__(self, **kwargs):
        super(IPythonParallelKernel, self).__init__(**kwargs)
        # add apply_request, in anticipation of upstream deprecation
        self.shell_handlers['apply_request'] = self.apply_request
        # set up data pub
        data_pub = self.shell.data_pub = self.data_pub_class(parent=self)
        self.shell.configurables.append(data_pub)
        data_pub.session = self.session
        data_pub.pub_socket = self.iopub_socket

    def init_metadata(self, parent):
        """init metadata dict, for execute/apply_reply"""
        return {
            'started': utcnow(),
            'dependencies_met' : True,
            'engine' : self.ident,
        }
    
    def finish_metadata(self, parent, metadata, reply_content):
        """Finish populating metadata.
        
        Run after completing a request handler.
        """
        # FIXME: remove ipyparallel-specific code
        metadata['status'] = reply_content['status']
        if reply_content['status'] == 'error':
            if reply_content['ename'] == 'UnmetDependency':
                metadata['dependencies_met'] = False
            metadata['engine_info'] = dict(
                engine_uuid=self.ident,
                engine_id=self.engine_id,
            )

        return metadata

    def execute_request(self, stream, ident, parent):
        """handle an execute_request - overridden for ipyparallel metadata
        
        Once ipykernel has init/finish_metadata, this should be removed.
        """

        try:
            content = parent[u'content']
            code = cast_unicode_py2(content[u'code'])
            silent = content[u'silent']
            store_history = content.get(u'store_history', not silent)
            user_expressions = content.get('user_expressions', {})
            allow_stdin = content.get('allow_stdin', False)
        except:
            self.log.error("Got bad msg: ")
            self.log.error("%s", parent)
            return

        stop_on_error = content.get('stop_on_error', True)
        
        md = self.init_metadata(parent)

        # Re-broadcast our input for the benefit of listening clients, and
        # start computing output
        if not silent:
            self.execution_count += 1
            self._publish_execute_input(code, parent, self.execution_count)

        reply_content = self.do_execute(code, silent, store_history,
                                        user_expressions, allow_stdin)
        # finish building metadata
        md = self.finish_metadata(parent, md, reply_content)
        
        # Flush output before sending the reply.
        sys.stdout.flush()
        sys.stderr.flush()

        # Send the reply.
        reply_content = json_clean(reply_content)

        reply_msg = self.session.send(stream, u'execute_reply',
                                      reply_content, parent, metadata=md,
                                      ident=ident)

        self.log.debug("%s", reply_msg)

        if not silent and reply_msg['content']['status'] == u'error' and stop_on_error:
            self._abort_queues()

    def apply_request(self, stream, ident, parent):
        try:
            content = parent[u'content']
            bufs = parent[u'buffers']
            msg_id = parent['header']['msg_id']
        except:
            self.log.error("Got bad msg: %s", parent, exc_info=True)
            return

        md = self.init_metadata(parent)

        reply_content, result_buf = self.do_apply(content, bufs, msg_id, md)

        # put 'ok'/'error' status in header, for scheduler introspection:
        md = self.finish_metadata(parent, md, reply_content)

        # flush i/o
        sys.stdout.flush()
        sys.stderr.flush()

        self.session.send(stream, u'apply_reply', reply_content,
                    parent=parent, ident=ident, buffers=result_buf, metadata=md)

    def do_apply(self, content, bufs, msg_id, reply_metadata):
        shell = self.shell
        try:
            working = shell.user_ns

            prefix = "_"+str(msg_id).replace("-","")+"_"

            f,args,kwargs = unpack_apply_message(bufs, working, copy=False)

            fname = getattr(f, '__name__', 'f')

            fname = prefix+"f"
            argname = prefix+"args"
            kwargname = prefix+"kwargs"
            resultname = prefix+"result"

            ns = { fname : f, argname : args, kwargname : kwargs , resultname : None }
            # print ns
            working.update(ns)
            code = "%s = %s(*%s,**%s)" % (resultname, fname, argname, kwargname)
            try:
                exec(code, shell.user_global_ns, shell.user_ns)
                result = working.get(resultname)
            finally:
                for key in ns:
                    working.pop(key)

            result_buf = serialize_object(result,
                buffer_threshold=self.session.buffer_threshold,
                item_threshold=self.session.item_threshold,
            )

        except BaseException as e:
            # invoke IPython traceback formatting
            shell.showtraceback()
            reply_content = {
                'traceback': [],
                'ename': unicode_type(type(e).__name__),
                'evalue': safe_unicode(e),
            }
            # get formatted traceback, which ipykernel recorded
            if hasattr(shell, '_last_traceback'):
                # ipykernel 4.4
                reply_content['traceback'] = shell._last_traceback or []
            elif hasattr(shell, '_reply_content'):
                # ipykernel <= 4.3
                if shell._reply_content and 'traceback' in shell._reply_content:
                    reply_content['traceback'] = shell._reply_content['traceback']
            else:
                self.log.warning("Didn't find a traceback where I expected to")
            shell._last_traceback = None
            e_info = dict(engine_uuid=self.ident, engine_id=self.int_id, method='apply')
            reply_content['engine_info'] = e_info

            self.send_response(self.iopub_socket, u'error', reply_content,
                                ident=self._topic('error'))
            self.log.info("Exception in apply request:\n%s", '\n'.join(reply_content['traceback']))
            result_buf = []
            reply_content['status'] = 'error'
        else:
            reply_content = {'status' : 'ok'}

        return reply_content, result_buf

    # Control messages for msgspec extensions:

    def abort_request(self, stream, ident, parent):
        """abort a specific msg by id"""
        self.log.warn("abort_request is deprecated in kernel_base. It is only part of IPython parallel")
        msg_ids = parent['content'].get('msg_ids', None)
        if isinstance(msg_ids, string_types):
            msg_ids = [msg_ids]
        if not msg_ids:
            self._abort_queues()
        for mid in msg_ids:
            self.aborted.add(str(mid))

        content = dict(status='ok')
        reply_msg = self.session.send(stream, 'abort_reply', content=content,
                parent=parent, ident=ident)
        self.log.debug("%s", reply_msg)


    def clear_request(self, stream, idents, parent):
        """Clear our namespace."""
        self.log.warn("clear_request is deprecated in kernel_base. It is only part of IPython parallel")
        self.shell.reset(False)
        content = dict(status='ok')
        self.session.send(stream, 'clear_reply', ident=idents, parent=parent,
                content = content)

