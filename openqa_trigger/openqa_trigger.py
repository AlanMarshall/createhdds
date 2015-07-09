#!/usr/bin/env python

import json
import re
import urlgrabber
import os.path
import sys
import subprocess
import argparse
import datetime
import logging
# We can at least find images and run OpenQA jobs without wikitcms
try:
    import wikitcms.wiki
except ImportError:
    wikitcms = None
import fedfind.release

from report_job_results import report_results

PERSISTENT = "/var/tmp/openqa_watcher.json"
ISO_PATH = "/var/lib/openqa/factory/iso/"
RUN_COMMAND = "/var/lib/openqa/script/client isos post " \
              "ISO=%s DISTRI=fedora VERSION=rawhide FLAVOR=%s ARCH=%s BUILD=%s"
DOCKER_COMMAND = "docker exec %s " + RUN_COMMAND
ARCHES = ['i386', 'x86_64']


class TriggerException(Exception):
    pass


# read last tested version from file
def read_last():
    logging.debug("reading latest checked version from %s", PERSISTENT)
    result = {}
    try:
        f = open(PERSISTENT, "r")
        json_raw = f.read()
        f.close()
        json_parsed = json.loads(json_raw)
    except IOError:
        logging.warning("cannot read file %s", PERSISTENT)
        return result, {}

    for arch in ARCHES:
        result[arch] = json_parsed.get(arch, None)
        logging.info("latest version for %s: %s", arch, result[arch])
    return result, json_parsed


def download_image(image):
    """Download a given image with a name that should be unique.
    Returns the filename of the image (not the path).
    """
    ver = image.version.replace(' ', '_')
    if image.imagetype == 'boot':
        isoname = "{0}_{1}_{2}_boot.iso".format(ver, image.payload, image.arch)
    else:
        isoname = "{0}_{1}".format(ver, image.filename)
    filename = os.path.join(ISO_PATH, isoname)
    if not os.path.isfile(filename):
        logging.info("downloading %s (%s) to %s", image.url, image.desc, filename)
        # Icky hack around a urlgrabber bug:
        # https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=715416
        urlgrabber.urlgrab(image.url.replace('https', 'http'), filename)
    else:
        logging.info("%s already exists", filename)
    return isoname


def run_openqa_jobs(isoname, flavor, arch, build, docker_container):
    """# run OpenQA 'isos' job on selected isoname, with given arch
    and a version string. If provided, use docker container docker_container
    that includes OpenQA WebUI. **NOTE**: the version passed to OpenQA as
    BUILD and is parsed back into the 'relval report-auto' arguments
    by report_job_results.py; it is expected to be in the form of a
    3-tuple on which join('_') has been run, and the three elements
    will be passed as --release, --compose and --milestone. Returns
    list of job IDs.
    """
    if docker_container:
        command = DOCKER_COMMAND % (docker_container, isoname, flavor, arch, build)
    else:
        command = RUN_COMMAND % (isoname, flavor, arch, build)

    logging.info("executing: %s", command)

    # starts OpenQA jobs
    output = subprocess.check_output(command.split())

    logging.debug("command executed")

    # read ids from OpenQA to wait for
    r = re.compile(r'ids => \[(?P<from>\d+)( \.\. (?P<to>\d+))?\]')
    match = r.search(output)
    if match and match.group('to'):
        from_i = int(match.group('from'))
        to_i = int(match.group('to')) + 1
        logging.info("planned jobs: %d to %d", from_i, to_i - 1)
        return range(from_i, to_i)
    elif match:
        job_id = int(match.group('from'))
        logging.info("planned job: %d", job_id)
        return [job_id]
    else:
        logging.info("no planned jobs")
        return []


def jobs_from_current(wiki, docker_container):
    """Schedule jobs against the 'current' release validation event
    (according to wikitcms) if we have not already. Returns a tuple,
    first value is the job list, second is the current event.
    """
    if not wiki:
        logging.warning("python-wikitcms is required for current validation event discovery.")
        return ([], None)
    last_versions, json_parsed = read_last()
    currev = wiki.current_event
    logging.info("current event: %s", currev.version)
    runarches = []
    for arch in ARCHES:
        last_version = last_versions.get(arch, None)
        if last_version and last_version >= currev.sortname:
            logging.info("skipped: %s: %s is newer or equal to %s",
                         arch, last_version, currev.sortname)
        else:
            runarches.append(arch)
            logging.debug("%s will be tested in version %s", arch, currev.sortname)
            json_parsed[arch] = currev.sortname

    jobs = []

    try:
        jobs = jobs_from_fedfind(currev.ff_release, runarches, docker_container)
        logging.info("planned jobs: %s", jobs)

        # write info about latest versions
        f = open(PERSISTENT, "w")
        f.write(json.dumps(json_parsed))
        f.close()
        logging.debug("written info about newest version")
    except TriggerException as e:
        logging.error("cannot run jobs: %s", e)

    return (jobs, currev)


def jobs_from_fedfind(ff_release, arches=ARCHES, docker_container=None):
    """Given a fedfind.Release object, find the ISOs we want and run
    jobs on them. arches is an iterable of arches to run on, if not
    specified, we'll use our constant.
    """
    # Find currently-testable images for our arches.
    jobs = []
    queries = (
        fedfind.release.Query('imagetype', ('boot', 'live')),
        fedfind.release.Query('arch', arches),
        fedfind.release.Query('payload', ('server', 'generic', 'workstation')))
    logging.debug("querying fedfind for images")
    images = ff_release.find_images(queries)

    if len(images) == 0:
        raise TriggerException("no available images")

    # Now schedule jobs. First, let's get the BUILD value for openQA.
    build = '_'.join((ff_release.release, ff_release.milestone, ff_release.compose))

    # Next let's schedule the 'universal' tests.
    # We have different images in different composes: nightlies only
    # have a generic boot.iso, TC/RC builds have Server netinst/boot
    # and DVD. We always want to run *some* tests -
    # default_boot_and_install at least - for all images we find, then
    # we want to run all the tests that are not image-dependent on
    # just one image. So we have a special 'universal' flavor and
    # product in openQA; all the image-independent test suites run for
    # that product. Here, we find the 'best' image we can for the
    # compose we're running on (a DVD if possible, a boot.iso or
    # netinst if not), and schedule the 'universal' jobs on that
    # image.
    for arch in arches:
        okimgs = (img for img in images if img.arch == arch and
                  any(img.imagetype == okt for okt in ('dvd', 'boot', 'netinst')))
        bestscore = 0
        bestimg = None
        for img in okimgs:
            if img.imagetype == 'dvd':
                score = 10
            else:
                score = 1
            if img.payload == 'generic':
                score += 5
            elif img.payload == 'server':
                score += 3
            elif img.payload == 'workstation':
                score += 1
            if score > bestscore:
                bestimg = img
                bestscore = score
        if not bestimg:
            logging.warn("no universal tests image found for %s", arch)
            continue
        logging.info("running universal tests for %s with %s", arch, bestimg.desc)
        isoname = download_image(bestimg)
        job_ids = run_openqa_jobs(isoname, 'universal', arch, build, docker_container)
        jobs.extend(job_ids)

    # Now schedule per-image jobs.
    for image in images:
        isoname = download_image(image)
        flavor = '_'.join((image.payload, image.imagetype))
        job_ids = run_openqa_jobs(isoname, flavor, image.arch, build, docker_container)
        jobs.extend(job_ids)
    return jobs


# SUB-COMMAND FUNCTIONS


def run_current(args, wiki):
    """run OpenQA for current release validation event, if we have
    not already done it.
    """
    logging.info("running on current release")
    jobs, _ = jobs_from_current(wiki, args.docker_container)
    # wait for jobs to finish and display results
    if jobs:
        logging.info("waiting for jobs: %s", jobs)
        report_results(jobs)
    logging.debug("finished")
    sys.exit()


def run_compose(args, wiki=None):
    """run OpenQA on a specified compose, optionally reporting results
    if a matching wikitcms ValidationEvent is found by relval/wikitcms
    """
    # get the fedfind release object
    try:
        logging.debug("querying fedfind on specific compose: %s %s %s", args.release,
                      args.milestone, args.compose)
        ff_release = fedfind.release.get_release(release=args.release, milestone=args.milestone,
                                                 compose=args.compose)
    except ValueError as err:
        logging.critical("compose %s %s %s was not found", args.release, args.milestone,
                         args.compose)
        sys.exit(err[0])

    logging.info("running on compose: %s", ff_release.version)
    jobs = []
    try:
        if args.arch:
            jobs = jobs_from_fedfind(ff_release, [args.arch], args.docker_container)
        else:
            jobs = jobs_from_fedfind(ff_release, docker_container=args.docker_container)
    except TriggerException as e:
        logging.error("cannot run jobs: %s", e)
    logging.info("planned jobs: %s", jobs)
    if args.submit_results:
        report_results(jobs)
    logging.debug("finished")
    sys.exit()


def run_all(args, wiki=None):
    """Do everything we can: test current validation event compose if
    it's new, amd test both Rawhide and Branched nightlies if they
    exist and aren't the same as the 'current' compose.
    """
    skip = ''
    logging.info("running all")

    # Run for 'current' validation event.
    logging.debug("running for current")
    (jobs, currev) = jobs_from_current(wiki, args.docker_container)
    logging.info("jobs from current validation event: %s", jobs)

    utcdate = datetime.datetime.utcnow()
    if args.yesterday:
        utcdate = utcdate - datetime.timedelta(days=1)
    if currev and currev.compose == utcdate.strftime('%Y%m%d'):
        # Don't schedule tests for the same compose as both "today's
        # nightly" and "current validation event"
        skip = currev.milestone
        logging.debug("skipping %s because it's both today's and current validation event", skip)

    # Run for day's Rawhide nightly (if not same as current event.)
    if skip.lower() != 'rawhide':
        try:
            logging.debug("running for rawhide")
            rawhide_ffrel = fedfind.release.get_release(release='Rawhide', compose=utcdate)
            rawjobs = jobs_from_fedfind(rawhide_ffrel, docker_container=args.docker_container)
            logging.info("jobs from rawhide %s: %s", rawhide_ffrel.version, rawjobs)
            jobs.extend(rawjobs)
        except ValueError as err:
            logging.error("rawhide image discovery failed: %s", err)
        except TriggerException as e:
            logging.error("cannot run jobs: %s", e)

    # Run for day's Branched nightly (if not same as current event.)
    # We must guess a release for Branched, fedfind cannot do so. Best
    # guess we can make is the same as the 'current' validation event
    # compose (this is why we have jobs_from_current return currev).
    if skip.lower() != 'branched':
        try:
            logging.debug("running for branched")
            branched_ffrel = fedfind.release.get_release(release=currev.release,
                                                         milestone='Branched', compose=utcdate)
            branchjobs = jobs_from_fedfind(branched_ffrel, docker_container=args.docker_container)
            logging.info("jobs from %s: %s", branched_ffrel.version, branchjobs)
            jobs.extend(branchjobs)
        except ValueError as err:
            logging.error("branched image discovery failed: %s", err)
        except TriggerException as e:
            logging.error("cannot run jobs: %s", e)
    if jobs:
        logging.info("waiting for jobs: %s", jobs)
        report_results(jobs)
    logging.debug("finished")
    sys.exit()


if __name__ == "__main__":
    test_help = "Operate on the staging wiki (for testing)"
    parser = argparse.ArgumentParser(description=(
        "Run OpenQA tests for a release validation test event."))
    subparsers = parser.add_subparsers()

    parser_current = subparsers.add_parser(
        'current', description="Run for the current event, if needed.")
    parser_current.set_defaults(func=run_current)

    parser_compose = subparsers.add_parser(
        'compose', description="Run for a specific compose (TC/RC or nightly)."
        " If a matching release validation test event can be found and "
        "--submit-results is passed, results will be reported.")
    parser_compose.add_argument(
        '-r', '--release', type=int, required=False, choices=range(12, 100),
        metavar="12-99", help="Release number of a specific compose to run "
        "against. Must be passed for validation event discovery to succeed.")
    parser_compose.add_argument(
        '-m', '--milestone', help="The milestone to operate on (Alpha, Beta, "
        "Final, Branched, Rawhide). Must be specified for a TC/RC; for a "
        "nightly, will be guessed if not specified", required=False,
        choices=['Alpha', 'Beta', 'Final', 'Branched', 'Rawhide'])
    parser_compose.add_argument(
        '-c', '--compose', help="The version to run for; either the compose "
        "(for a TC/RC), or the date (for a nightly build)", required=False,
        metavar="{T,R}C1-19 or YYYYMMDD")
    parser_compose.add_argument(
        '-a', '--arch', help="The arch to run for", required=False,
        choices=('x86_64', 'i386'))
    parser_compose.add_argument(
        '-s', '--submit-results', help="Submit the results to the release "
        "validation event for this compose, if possible", required=False,
        action='store_true')
    parser_compose.set_defaults(func=run_compose)

    parser_all = subparsers.add_parser(
        'all', description="Run for the current validation event (if needed) "
        "and today's Rawhide and Branched nightly's (if found). 'Today' is "
        "calculated for the UTC time zone, no matter the system timezone.")
    parser_all.add_argument(
        '-y', '--yesterday', help="Run on yesterday's nightlies, not today's",
        required=False, action='store_true')
    parser_all.set_defaults(func=run_all)

    parser.add_argument(
        '-d', '--docker-container', help="If given, run tests using "
        "specified docker container")
    parser.add_argument(
        '-t', '--test', help=test_help, required=False, action='store_true')
    parser.add_argument(
        '-f', '--log-file', help="If given, log into specified file. When not provided, stdout"
        " is used", required=False)
    parser.add_argument(
        '-l', '--log-level', help="Specify log level to be outputted", required=False)
    parser.add_argument('-i', '--iso-directory', help="Directory for downloading isos, default"
                        " is %s" % PERSISTENT, required=False)

    args = parser.parse_args()

    if args.log_level:
        log_level = getattr(logging, args.log_level.upper(), None)
        if not isinstance(log_level, int):
            log_level = logging.WARNING
    else:
        log_level = logging.WARNING
    if args.log_file:
        logging.basicConfig(format="%(levelname)s:%(name)s:%(asctime)s:%(message)s",
                            filename=args.log_file, level=log_level)
    else:
        logging.basicConfig(level=log_level)

    if args.iso_directory:
        ISO_PATH = args.iso_directory

    wiki = None
    if args.test:
        logging.debug("using test wiki")
        if wikitcms:
            wiki = wikitcms.wiki.Wiki(('https', 'stg.fedoraproject.org'), '/w/')
        else:
            logging.warn("wikitcms not found, reporting to wiki disabled")
    else:
        if wikitcms:
            wiki = wikitcms.wiki.Wiki(('https', 'fedoraproject.org'), '/w/')
        else:
            logging.warn("wikitcms not found, reporting to wiki disabled")
    args.func(args, wiki)
