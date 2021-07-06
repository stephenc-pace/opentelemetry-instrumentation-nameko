# -*- coding: utf-8 -*-
"""
This module patches the Nameko ServiceContainer so that every entrypoint that fires
generates a span with helpful defaults.

The kind, name, attributes and status of the span are determined by the
EntrypointAdapter class. More specialised versions can be provided by passing an
appropriate dictionary as `entrypoint_adapters` when invoking
`NamekoInstrumentor.instrument()`.

For example:

    entrypoint_to_adapter_map = {
        "my.custom.EntrypointType": "my.custom.EntrypointAdapter"
    }

    instrumentor = NamekoInstrumentor()
    instrumentor.instrument(entrypoint_adapters=entrypoint_to_adapter_map)

"""
import inspect
import socket
import warnings
from collections import defaultdict
from functools import partial
from traceback import format_exception
from weakref import WeakKeyDictionary

import nameko.containers
from nameko.utils import get_redacted_args
from opentelemetry import context, trace
from opentelemetry.instrumentation.utils import unwrap
from opentelemetry.propagate import extract
from opentelemetry.trace.status import Status, StatusCode
from opentelemetry.util._time import _time_ns
from wrapt import wrap_function_wrapper

from nameko_opentelemetry import utils


DEFAULT_ADAPTERS = {
    "nameko.rpc.Rpc": ("nameko_opentelemetry.rpc.RpcEntrypointAdapter"),
    "nameko.web.handlers.HttpRequestHandler": (
        "nameko_opentelemetry.http.HttpEntrypointAdapter"
    ),
    "nameko.events.EventHandler": (
        "nameko_opentelemetry.events.EventHandlerEntrypointAdapter"
    ),
    "nameko.messaging.Consumer": (
        "nameko_opentelemetry.messaging.ConsumerEntrypointAdapter"
    ),
    "nameko.timer.Timer": ("nameko_opentelemetry.timer.TimerEntrypointAdapter"),
}

active_spans = WeakKeyDictionary()
adapter_types = defaultdict(lambda: EntrypointAdapter)


class EntrypointAdapter:
    """ Default entrypoint adapter. This implementation is used unless there's
    a more specific adapter set for the firing entrypoint's type.
    """

    span_kind = trace.SpanKind.SERVER

    def __init__(self, worker_ctx, config):
        self.worker_ctx = worker_ctx
        self.config = config

    def get_span_name(self):
        return (
            f"{self.worker_ctx.service_name}.{self.worker_ctx.entrypoint.method_name}"
        )

    def get_metadata(self):
        return self.worker_ctx.context_data

    def exception_was_expected(self, exc):
        expected_exceptions = getattr(
            self.worker_ctx.entrypoint, "expected_exceptions", None
        )
        expected_exceptions = expected_exceptions or tuple()
        return isinstance(exc, expected_exceptions)

    def start_span(self, span):
        if span.is_recording():
            span.set_attributes(self.get_attributes())

    def end_span(self, span, result, exc_info):
        if span.is_recording():

            if exc_info:
                span.record_exception(
                    exc_info[1],
                    escaped=True,
                    attributes=self.get_exception_attributes(exc_info),
                )
            else:
                span.set_attributes(self.get_result_attributes(result))

            status = self.get_status(result, exc_info)
            span.set_status(status)

    def get_attributes(self):
        """ Common attributes for most entrypoints.
        """
        entrypoint = self.worker_ctx.entrypoint

        attributes = {
            "service_name": self.worker_ctx.service_name,
            "entrypoint_type": type(entrypoint).__name__,
            "method_name": entrypoint.method_name,
            "active_workers": self.worker_ctx.container._worker_pool.running(),
            "available_workers": self.worker_ctx.container._worker_pool.free(),
        }

        if self.config.get("send_headers"):
            attributes.update(
                {
                    "context_data": utils.serialise_to_string(
                        self.worker_ctx.data
                    ),  # TODO scrub!
                }
            )

        if self.config.get("send_request_payloads"):

            if getattr(entrypoint, "sensitive_arguments", None):
                call_args = get_redacted_args(
                    entrypoint, *self.worker_ctx.args, **self.worker_ctx.kwargs
                )
                redacted = True
            else:
                method = getattr(
                    entrypoint.container.service_cls, entrypoint.method_name
                )
                call_args = inspect.getcallargs(
                    method, None, *self.worker_ctx.args, **self.worker_ctx.kwargs
                )
                del call_args["self"]
                redacted = False

            call_args, truncated = utils.truncate(
                utils.serialise_to_string(call_args),
                max_len=self.config.get("truncate_max_length"),
            )

            attributes.update(
                {
                    "call_args": call_args,
                    "call_args_redacted": str(redacted),
                    "call_args_truncated": str(truncated),
                }
            )

        return attributes

    def get_exception_attributes(self, exc_info):
        """ Additional attributes to save alongside a worker exception.
        """
        exc_type, exc, _ = exc_info

        try:
            stacktrace = "\n".join(format_exception(*exc_info))
        except Exception:
            stacktrace = "Exception occurred on stacktrace formatting"

        return {
            "exception.stacktrace": stacktrace,
            "exception.expected": str(self.exception_was_expected(exc)),
        }

    def get_result_attributes(self, result):
        """ Attributes describing the entrypoint method result.
        """
        if self.config.get("send_response_payloads"):
            attributes = {"result": utils.safe_for_serialisation(result or "")}
        else:
            attributes = {}
        return attributes

    def get_status(self, result, exc_info):
        """ Span status for this worker.
        """
        if exc_info:
            exc_type, exc, _ = exc_info

            if not self.exception_was_expected(exc):
                return Status(
                    StatusCode.ERROR,
                    description="{}: {}".format(type(exc).__name__, exc),
                )

        return Status(StatusCode.OK)


def adapter_factory(worker_ctx, config):
    adapter_class = adapter_types[type(worker_ctx.entrypoint)]
    return adapter_class(worker_ctx, config)


def worker_setup(tracer, config, wrapped, instance, args, kwargs):
    """ Wrap nameko.containers.ServiceContainer._worker_setup.

    Creates a new span for each entrypoint that fires. The name of the
    span and its attributes are determined by the entrypoint "adapter"
    that is configured for that entrypoint, or the default implementation. 
    """
    (worker_ctx,) = args

    adapter = adapter_factory(worker_ctx, config)
    ctx = extract(adapter.get_metadata())
    token = context.attach(ctx)

    span = tracer.start_span(
        adapter.get_span_name(),
        kind=adapter.span_kind,
        attributes={"hostname": socket.gethostname()},
        start_time=_time_ns(),
    )
    # don't automatically record the exception or set status, because
    # we do that in the entrypoint adapter's `end_span` method
    activation = trace.use_span(
        span, record_exception=False, set_status_on_exception=False
    )
    activation.__enter__()
    active_spans[worker_ctx] = (activation, span, token, adapter)

    adapter.start_span(span)


def worker_result(tracer, config, wrapped, instance, args, kwargs):
    """ Wrap nameko.containers.ServiceContainer._worker_result.

    Finds the existing span for this worker and closes it. Additional
    attributes and status are set by the configured entrypoint adapter.
    """
    (worker_ctx, result, exc_info) = args

    activated = active_spans.get(worker_ctx)
    if not activated:
        # something went wrong when starting the span; nothing more to do
        warnings.warn("worker result when no active span")
        return

    activation, span, token, adapter = activated

    adapter.end_span(span, result, exc_info)

    if exc_info is None:
        activation.__exit__(None, None, None)
    else:
        activation.__exit__(*exc_info)

    span.end(_time_ns())
    context.detach(token)


def instrument(tracer, config):

    # set up entrypoint adapters
    adapter_config = DEFAULT_ADAPTERS.copy()
    adapter_config.update(config.get("entrypoint_adapters", {}))

    for entrypoint_path, adapter_path in adapter_config.items():
        entrypoint_class = utils.import_by_path(entrypoint_path)
        adapter_class = utils.import_by_path(adapter_path)
        adapter_types[entrypoint_class] = adapter_class

    # apply patches
    wrap_function_wrapper(
        "nameko.containers",
        "ServiceContainer._worker_setup",
        partial(worker_setup, tracer, config),
    )
    wrap_function_wrapper(
        "nameko.containers",
        "ServiceContainer._worker_result",
        partial(worker_result, tracer, config),
    )


def uninstrument():
    unwrap(nameko.containers.ServiceContainer, "worker_setup")
    unwrap(nameko.containers.ServiceContainer, "worker_result")
