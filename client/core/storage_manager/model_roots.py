"""External models contribute known files, not a scan of their sibling folders."""
import os


def external_model_files(app):
    from core.transcription import external_models, model_catalog, model_store, whisper_cpp_catalog

    paths = []
    stored = app.get_strict("transcription_models_dir")
    catalogues = tuple(model_catalog.list_models()) + tuple(whisper_cpp_catalog.list_models())
    if isinstance(stored, str) and stored:
        for model in catalogues:
            directory = model_store.model_dir(stored, model.id)
            for name, _size in model.files:
                paths.extend((os.path.join(directory, name), os.path.join(directory, name + ".part")))
    references, readable = external_models.read_references(app)
    if not readable:
        raise OSError("external model references unavailable")
    known = set(external_models.REQUIRED_FILES + external_models.VOCABULARY_FILES)
    known.update(name for model in model_catalog.list_models() for name, _size in model.files)
    for reference in references:
        if reference.is_file:
            paths.append(reference.path)
        else:
            paths.extend(os.path.join(reference.path, name) for name in known)
    return tuple(dict.fromkeys(paths))
