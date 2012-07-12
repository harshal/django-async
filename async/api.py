"""
    Schedule the execution of an async task.
"""
from datetime import datetime
from simplejson import dumps

from async.models import Error, Job
from async.utils import full_name


def schedule(function, args = None, kwargs = None,
        run_after= None, meta = None):
    """Schedule a tast for execution.
    """
    job = Job(
        name=full_name(function),
            args=dumps(args or []), kwargs=dumps(kwargs or {}),
        meta=dumps(meta or {}), scheduled=run_after)
    job.save()
    return job


def deschedule(function, args = None, kwargs = None):
    """Remove any instances of the job from the queue.
    """
    job = Job(
        name=full_name(function),
            args=dumps(args or []), kwargs=dumps(kwargs or {}))
    mark_executed = Job.objects.filter(identity=unicode(job))
    mark_executed.update(executed=datetime.now())


def health():
    """Return information about the health of the queue in a format that
    can be turned into JSON.
    """
    output = {'queue': {}, 'errors': {}}
    output['queue']['length'] = Job.objects.all().count()
    output['queue']['executed'] = Job.objects.filter(executed=None).count()
    output['errors']['number'] = Error.objects.all().count()
    return output

