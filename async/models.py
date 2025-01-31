"""
    Django Async models.
"""
from datetime import  timedelta
from django.db import models, transaction
from django.db.models import Count, Q
try:
    # No name 'timezone' in module 'django.utils'
    # pylint: disable=E0611
    from django.utils import timezone
except ImportError: # pragma: no cover
    from datetime import datetime as timezone
# No name 'sha1' in module 'hashlib'
# pylint: disable=E0611
from hashlib import sha1
from simplejson import dumps, loads
from traceback import format_exc

from async.logger import _logger
from async.utils import object_at_end_of_path, non_unicode_kwarg_keys
from async.command_stats import StatBaseCommand
from django.core.exceptions import ValidationError


class Group(models.Model):
    """
        A group for jobs that need to be executed.
    """
    reference = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)
    final = models.ForeignKey('Job', blank=True, null=True,
        related_name='ends')

    def __unicode__(self):
        return u'%s' % self.reference

    def save(self, *args, **kwargs):
        # We can't create a new group with that reference
        # if the old group still has jobs that haven't executed.
        if Job.objects.filter(group__reference=self.reference).filter(
                    Q(executed__isnull=True) & Q(cancelled__isnull=True)
                ).exclude(group__id=self.id).count() > 0:
            raise ValidationError(
                "Group reference [%s] still has unexecuted jobs." %
                    self.reference)
        result = super(Group, self).save(*args, **kwargs)
        if self.final and self.final.group != self:
            self.final.group = self
            self.final.save()
        return result

    def on_completion(self, job):
        """Set a job to be the one that executes when the other jobs
        in the group have completed.
        """
        self.final = job
        self.save()

    def estimate_execution_duration(self):
        """Estimate of the total amount of time (in seconds) that the group
        will take to execute.
        """
        result = self.jobs.aggregate(
            job_count=Count('id'), executed_job_count=Count('executed'),
            cancelled_job_count=Count('cancelled'))
        total_jobs = result['job_count']
        total_executed_jobs = result['executed_job_count']
        total_cancelled_jobs = result['cancelled_job_count']
        total_done = total_executed_jobs + total_cancelled_jobs
        if total_jobs > 0:
            # Don't allow to calculate if executed jobs are not valid.
            if total_done == 0:
                return None, None, None
            elif not self.has_completed():
                # Some jobs are unexecuted.
                time_consumed = timezone.now() - self.created
                estimated_time = timedelta(seconds=(
                    time_consumed.seconds/float(total_done))
                        * total_jobs)
                remaining = estimated_time - time_consumed
            else:
                # All jobs in group are executed.
                estimated_time = (
                    self.latest_executed_job().executed - self.created)
                time_consumed = estimated_time
                remaining = timedelta(seconds=0)
            return estimated_time, remaining, time_consumed
        else:
            return None, None, None

    def latest_executed_job(self):
        """When the last executed job in the group was completed.
        """
        if self.jobs.filter(executed__isnull=False).count():
            return self.jobs.filter(executed__isnull=False).latest('executed')

    def has_completed(self, exclude=None):
        """Return True if all jobs are either executed or cancelled.
        """
        job_query = self.jobs.all()
        if exclude:
            job_query = job_query.exclude(pk=exclude.pk)
        return (self.jobs.all().count() > 0 and
            job_query.filter(
                Q(executed__isnull=True) & Q(cancelled__isnull=True)
            ).count() == 0)

    @staticmethod
    def latest_group_by_reference(reference):
        """
            Fetch the latest group with the requested reference. It will
            create a new group if necessary.
        """
        try:
            group = Group.objects.filter(
                reference=reference).latest('created')
            if group.has_completed():
                # The found group is either fully executed or cancelled
                # so make a new one
                group = Group.objects.create(
                    reference=reference, description=group.description)
        except Group.DoesNotExist:
            group = Group.objects.create(reference=reference)
        return group

class JobArchive(models.Model):
    job_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=100, blank=False)
    args = models.TextField()
    kwargs = models.TextField()
    meta = models.TextField()
    result = models.TextField(blank=True)

    priority = models.IntegerField()
    identity = models.CharField(max_length=100, blank=False, db_index=True)

    added = models.DateTimeField(null=True, blank=True,)
    scheduled = models.DateTimeField(null=True, blank=True,
        help_text="If not set, will be executed ASAP")
    started = models.DateTimeField(null=True, blank=True)
    executed = models.DateTimeField(null=True, blank=True)
    cancelled = models.DateTimeField(null=True, blank=True)

    fairness = models.IntegerField(null=True, blank=True)

class ErrorArchive(models.Model):
    """
        Recorded when an error happens during execution of a job.
    """
    error_id = models.IntegerField(primary_key=True)
    job = models.ForeignKey(JobArchive, related_name='errors')
    executed = models.DateTimeField(auto_now_add=True)
    exception = models.TextField()
    traceback = models.TextField()

    def __unicode__(self):
        return u'%s : %s' % (self.executed, self.exception)



class Job(models.Model):
    """
        An asynchronous task that is to be executed.
    """
    name = models.CharField(max_length=100, blank=False)
    args = models.TextField()
    kwargs = models.TextField()
    meta = models.TextField()
    result = models.TextField(blank=True)

    priority = models.IntegerField()
    identity = models.CharField(max_length=100, blank=False, db_index=True)

    added = models.DateTimeField(auto_now_add=True)
    scheduled = models.DateTimeField(null=True, blank=True,
        help_text="If not set, will be executed ASAP")
    started = models.DateTimeField(null=True, blank=True)
    executed = models.DateTimeField(null=True, blank=True)
    cancelled = models.DateTimeField(null=True, blank=True)

    group = models.ForeignKey(Group, related_name='jobs',
        null=True, blank=True)
    fairness = models.IntegerField(default=-1, null=True, blank=True)
    def __unicode__(self):
        # __unicode__: Instance of 'bool' has no 'items' member
        # pylint: disable=E1103
        args = ', '.join([repr(s) for s in loads(self.args)] +
            ['%s=%s' % (k, repr(v)) for k, v in loads(self.kwargs).items()])
        return u'%s(%s)' % (self.name, args)

    def save(self, *a, **kw):
        # Stop us from cheating by adding the new jobs to the old group.
        # Checking if group obj got passed and current job is not in that group
        if self.group and self not in self.group.jobs.all():
            # Cannot add current job to latest group that have an executed job.
            if self.group.jobs.filter(
                        Q(executed__isnull=False) | Q(cancelled__isnull=False)
                    ).count() > 0:
                raise ValidationError(
                    "Cannot add job [%s] to group [%s] because this group "
                        "has executed jobs." %
                            (self.name, self.group.reference))
        self.identity = sha1(unicode(self)).hexdigest()
        return super(Job, self).save(*a, **kw)

    def execute(self, **_meta):
        """
            Run the job using the specified meta values to control the
            execution.
        """
        sbc = StatBaseCommand()
        sbc.command = self.name
        try:
            _logger.info("%s %s", self.id, unicode(self))
            args = loads(self.args)
            kwargs = non_unicode_kwarg_keys(loads(self.kwargs))
            #Add priority and fairness to kwargs.
            kwargs['priority'] = self.priority
            kwargs['fairness'] = self.fairness
            function = object_at_end_of_path(self.name)
            _logger.debug(u"%s resolved to %s" % (self.name, function))
            def execute():
                """Execute the database updates in one transaction.
                """
                self.started = timezone.now()
                result = function(*args, **kwargs)
                self.executed = timezone.now()
                self.result = dumps(result)
                self.save()
                sbc._stats(success=True, stats_type="execute")
                return result
            return transaction.commit_on_success(execute)()
        except Exception, exception:
            self.started = None
            errors = 1 + self.errors.count()
            self.scheduled = (timezone.now() +
                timedelta(seconds=60 * pow(errors, 1.6)))
            self.priority = self.priority - 1
            _logger.error(
                "Job %s failed. Rescheduled for %s after %s error(s). "
                    "New priority is %s."
                    "Exception is %s."
                    "Trace is %s",
                self.id, self.scheduled, errors, self.priority, repr(exception), format_exc())
            def record():
                """Local function allows us to wrap these updates into a
                transaction.
                """
                Error.objects.create(job=self, exception=repr(exception),
                    traceback=format_exc())
                self.save()
            transaction.commit_on_success(record)()
            sbc._stats(success=False, stats_type="execute")
            raise


class Error(models.Model):
    """
        Recorded when an error happens during execution of a job.
    """
    job = models.ForeignKey(Job, related_name='errors')
    executed = models.DateTimeField(auto_now_add=True)
    exception = models.TextField()
    traceback = models.TextField()

    def __unicode__(self):
        return u'%s : %s' % (self.executed, self.exception)

