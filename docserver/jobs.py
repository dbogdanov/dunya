import importlib
import os
import json
import logging

from docserver import models
import celery

from django.conf import settings

class DatabaseLogHandler(logging.Handler):
    def handle(self, record):
        documentid = getattr(record, "documentid", None)
        sourcefileid = getattr(record, "sourcefileid", None)
        modulename = getattr(record, "modulename", None)
        moduleversion = getattr(record, "moduleversion")

        modv = None
        if modulename and moduleversion:
            try:
                mod = models.Module.objects.get(module=modulename)
                modv = mod.moduleversion_set.get(version=moduleversion)
            except (models.Module.DoesNotExist, models.ModuleVersion.DoesNotExist):
                pass

        sourcef = None
        if sourcefileid:
            try:
                sourcef = models.SourceFile.objects.get(pk=int(sourcefileid))
            except models.SourceFile.DoesNotExist:
                pass
        doc = None
        if documentid:
            try:
                doc = models.Document.objects.get(pk=int(documentid))
                models.DocumentLogMessage.objects.create(document=doc, moduleversion=modv, sourcefile=sourcef, level=record.levelname, message=record.getMessage())
            except models.Document.DoesNotExist:
                print "no document, can't create a log file"
        else:
            print "no document, can't create a log file"

logger = logging.getLogger("essentia")
logger.setLevel(logging.DEBUG)
logger.addHandler(DatabaseLogHandler())

def _get_module_instance_by_path(modulepath):
    mod, clsname = modulepath.rsplit(".", 1)
    package = importlib.import_module(mod)
    cls = getattr(package, clsname)
    return cls()

def create_module(modulepath, collections):
    instance = _get_module_instance_by_path(modulepath)
    try:
        sourcetype = models.SourceFileType.objects.get(extension=instance.__sourcetype__)
    except models.SourceFileType.DoesNotExist as e:
        raise Exception("Cannot find source file type '%s'" % instance.__sourcetype__, e)

    module = models.Module.objects.create(name=modulepath.rsplit(".", 1)[1],
                    slug=instance.__slug__,
                    depends=instance.__depends__,
                    module=modulepath,
                    source_type=sourcetype)
    module.collections.add(*collections)
    get_latest_module_version(module.pk)

def get_latest_module_version(themod_id=None):
    if themod_id:
        modules = [models.Module.objects.get(pk=themod_id)]
    else:
        modules = models.Module.objects.all()
    for m in modules:
        instance = _get_module_instance_by_path(m.module)
        version = instance.__version__
        v = "%s" % version

        versions = m.moduleversion_set.filter(version=version)
        if not len(versions):
            models.ModuleVersion.objects.create(module=m, version=v)

def _save_file(collection, recordingid, slug, extension, data):
    fdir = os.path.join(settings.AUDIO_ROOT, collection, recordingid)
    try:
        os.makedirs(fdir)
    except OSError:
        pass
    fname = "%s.%s" % (slug, extension)

    fullname = os.path.join(fdir, fname)
    fp = open(fullname, "wb")
    json.dump(data, fp)
    return fullname

@celery.task
def process_document(documentid, moduleversionid):
    version = models.ModuleVersion.objects.get(pk=moduleversionid)
    module = version.module
    instance = _get_module_instance_by_path(module.module)

    document = models.Document.objects.get(pk=documentid)

    sfiles = document.sourcefiles.filter(file_type=module.source_type)
    if len(sfiles):
        # TODO: If there is more than 1 source file
        s = sfiles[0]
        fname = s.path.encode("utf-8")
        results = instance.process_document(document.pk, s.pk, fname)

        collectionid = document.collection.collectionid
        moduleslug = module.slug
        for name, contents in results.items():
            outputdata = instance.__output__[name]
            extension = outputdata["extension"]
            mimetype = outputdata["mimetype"]
            df = models.DerivedFile.objects.create(document=document, derived_from=s,
                    module_version=version, outputname=name, extension=extension, mimetype=mimetype)

            if not isinstance(contents, list):
                contents = [contents]
            for i, part in enumerate(contents, 1):
                saved_name = _save_file(collectionid, document.external_identifier, moduleslug, extension, result)
                df.save_part(i, saved_name, len(result))

def run_module(moduleid):
    module = models.Module.objects.get(pk=moduleid)
    collections = module.collections.all()
    for c in collections:
        run_module_on_collection.delay(c.pk, module.pk)

@celery.task
def run_module_on_collection(collectionid, moduleid):
    collection = models.Collection.objects.get(pk=collectionid)
    module = models.Module.objects.get(pk=moduleid)
    version = module.get_latest_version()
    print "running module", module, "on collection", collection
    if version:
        print "version", version
        # All documents that don't already have a derived file for this module version
        docs = models.Document.objects.filter(sourcefiles__file_type=module.source_type).exclude(derivedfiles__module_version=version)
        for d in docs:
            print "  document", d
            process_document(d.pk, version.pk)
