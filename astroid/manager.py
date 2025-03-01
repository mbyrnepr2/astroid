# Licensed under the LGPL: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.en.html
# For details: https://github.com/PyCQA/astroid/blob/main/LICENSE
# Copyright (c) https://github.com/PyCQA/astroid/blob/main/CONTRIBUTORS.txt

"""astroid manager: avoid multiple astroid build of a same module when
possible by providing a class responsible to get astroid representation
from various source and using a cache of built modules)
"""

import os
import types
import zipimport
from importlib.util import find_spec, module_from_spec
from typing import TYPE_CHECKING, ClassVar, List, Optional

from astroid.const import BRAIN_MODULES_DIRECTORY
from astroid.exceptions import AstroidBuildingError, AstroidImportError
from astroid.interpreter._import import spec
from astroid.modutils import (
    NoSourceFile,
    file_info_from_modpath,
    get_source_file,
    is_module_name_part_of_extension_package_whitelist,
    is_python_source,
    is_standard_module,
    load_module_from_name,
    modpath_from_file,
)
from astroid.transforms import TransformVisitor
from astroid.typing import AstroidManagerBrain

if TYPE_CHECKING:
    from astroid import nodes

ZIP_IMPORT_EXTS = (".zip", ".egg", ".whl", ".pyz", ".pyzw")


def safe_repr(obj):
    try:
        return repr(obj)
    except Exception:  # pylint: disable=broad-except
        return "???"


class AstroidManager:
    """Responsible to build astroid from files or modules.

    Use the Borg (singleton) pattern.
    """

    name = "astroid loader"
    brain: AstroidManagerBrain = {
        "astroid_cache": {},
        "_mod_file_cache": {},
        "_failed_import_hooks": [],
        "always_load_extensions": False,
        "optimize_ast": False,
        "extension_package_whitelist": set(),
        "_transform": TransformVisitor(),
    }
    max_inferable_values: ClassVar[int] = 100

    def __init__(self):
        # NOTE: cache entries are added by the [re]builder
        self.astroid_cache = AstroidManager.brain["astroid_cache"]
        self._mod_file_cache = AstroidManager.brain["_mod_file_cache"]
        self._failed_import_hooks = AstroidManager.brain["_failed_import_hooks"]
        self.always_load_extensions = AstroidManager.brain["always_load_extensions"]
        self.optimize_ast = AstroidManager.brain["optimize_ast"]
        self.extension_package_whitelist = AstroidManager.brain[
            "extension_package_whitelist"
        ]
        self._transform = AstroidManager.brain["_transform"]

    @property
    def register_transform(self):
        # This and unregister_transform below are exported for convenience
        return self._transform.register_transform

    @property
    def unregister_transform(self):
        return self._transform.unregister_transform

    @property
    def builtins_module(self):
        return self.astroid_cache["builtins"]

    def visit_transforms(self, node):
        """Visit the transforms and apply them to the given *node*."""
        return self._transform.visit(node)

    def ast_from_file(self, filepath, modname=None, fallback=True, source=False):
        """given a module name, return the astroid object"""
        try:
            filepath = get_source_file(filepath, include_no_ext=True)
            source = True
        except NoSourceFile:
            pass
        if modname is None:
            try:
                modname = ".".join(modpath_from_file(filepath))
            except ImportError:
                modname = filepath
        if (
            modname in self.astroid_cache
            and self.astroid_cache[modname].file == filepath
        ):
            return self.astroid_cache[modname]
        if source:
            # pylint: disable=import-outside-toplevel; circular import
            from astroid.builder import AstroidBuilder

            return AstroidBuilder(self).file_build(filepath, modname)
        if fallback and modname:
            return self.ast_from_module_name(modname)
        raise AstroidBuildingError("Unable to build an AST for {path}.", path=filepath)

    def ast_from_string(self, data, modname="", filepath=None):
        """Given some source code as a string, return its corresponding astroid object"""
        # pylint: disable=import-outside-toplevel; circular import
        from astroid.builder import AstroidBuilder

        return AstroidBuilder(self).string_build(data, modname, filepath)

    def _build_stub_module(self, modname):
        # pylint: disable=import-outside-toplevel; circular import
        from astroid.builder import AstroidBuilder

        return AstroidBuilder(self).string_build("", modname)

    def _build_namespace_module(self, modname: str, path: List[str]) -> "nodes.Module":
        # pylint: disable=import-outside-toplevel; circular import
        from astroid.builder import build_namespace_package_module

        return build_namespace_package_module(modname, path)

    def _can_load_extension(self, modname: str) -> bool:
        if self.always_load_extensions:
            return True
        if is_standard_module(modname):
            return True
        return is_module_name_part_of_extension_package_whitelist(
            modname, self.extension_package_whitelist
        )

    def ast_from_module_name(self, modname, context_file=None):
        """given a module name, return the astroid object"""
        if modname in self.astroid_cache:
            return self.astroid_cache[modname]
        if modname == "__main__":
            return self._build_stub_module(modname)
        if context_file:
            old_cwd = os.getcwd()
            os.chdir(os.path.dirname(context_file))
        try:
            found_spec = self.file_from_module_name(modname, context_file)
            if found_spec.type == spec.ModuleType.PY_ZIPMODULE:
                module = self.zip_import_data(found_spec.location)
                if module is not None:
                    return module

            elif found_spec.type in (
                spec.ModuleType.C_BUILTIN,
                spec.ModuleType.C_EXTENSION,
            ):
                if (
                    found_spec.type == spec.ModuleType.C_EXTENSION
                    and not self._can_load_extension(modname)
                ):
                    return self._build_stub_module(modname)
                try:
                    module = load_module_from_name(modname)
                except Exception as e:
                    raise AstroidImportError(
                        "Loading {modname} failed with:\n{error}",
                        modname=modname,
                        path=found_spec.location,
                    ) from e
                return self.ast_from_module(module, modname)

            elif found_spec.type == spec.ModuleType.PY_COMPILED:
                raise AstroidImportError(
                    "Unable to load compiled module {modname}.",
                    modname=modname,
                    path=found_spec.location,
                )

            elif found_spec.type == spec.ModuleType.PY_NAMESPACE:
                return self._build_namespace_module(
                    modname, found_spec.submodule_search_locations
                )
            elif found_spec.type == spec.ModuleType.PY_FROZEN:
                if found_spec.location is None:
                    return self._build_stub_module(modname)
                # For stdlib frozen modules we can determine the location and
                # can therefore create a module from the source file
                return self.ast_from_file(found_spec.location, modname, fallback=False)

            if found_spec.location is None:
                raise AstroidImportError(
                    "Can't find a file for module {modname}.", modname=modname
                )

            return self.ast_from_file(found_spec.location, modname, fallback=False)
        except AstroidBuildingError as e:
            for hook in self._failed_import_hooks:
                try:
                    return hook(modname)
                except AstroidBuildingError:
                    pass
            raise e
        finally:
            if context_file:
                os.chdir(old_cwd)

    def zip_import_data(self, filepath):
        if zipimport is None:
            return None

        # pylint: disable=import-outside-toplevel; circular import
        from astroid.builder import AstroidBuilder

        builder = AstroidBuilder(self)
        for ext in ZIP_IMPORT_EXTS:
            try:
                eggpath, resource = filepath.rsplit(ext + os.path.sep, 1)
            except ValueError:
                continue
            try:
                # pylint: disable-next=no-member
                importer = zipimport.zipimporter(eggpath + ext)
                zmodname = resource.replace(os.path.sep, ".")
                if importer.is_package(resource):
                    zmodname = zmodname + ".__init__"
                module = builder.string_build(
                    importer.get_source(resource), zmodname, filepath
                )
                return module
            except Exception:  # pylint: disable=broad-except
                continue
        return None

    def file_from_module_name(self, modname, contextfile):
        try:
            value = self._mod_file_cache[(modname, contextfile)]
        except KeyError:
            try:
                value = file_info_from_modpath(
                    modname.split("."), context_file=contextfile
                )
            except ImportError as e:
                value = AstroidImportError(
                    "Failed to import module {modname} with error:\n{error}.",
                    modname=modname,
                    # we remove the traceback here to save on memory usage (since these exceptions are cached)
                    error=e.with_traceback(None),
                )
            self._mod_file_cache[(modname, contextfile)] = value
        if isinstance(value, AstroidBuildingError):
            # we remove the traceback here to save on memory usage (since these exceptions are cached)
            raise value.with_traceback(None)
        return value

    def ast_from_module(self, module: types.ModuleType, modname: Optional[str] = None):
        """given an imported module, return the astroid object"""
        modname = modname or module.__name__
        if modname in self.astroid_cache:
            return self.astroid_cache[modname]
        try:
            # some builtin modules don't have __file__ attribute
            filepath = module.__file__
            if is_python_source(filepath):
                return self.ast_from_file(filepath, modname)
        except AttributeError:
            pass

        # pylint: disable=import-outside-toplevel; circular import
        from astroid.builder import AstroidBuilder

        return AstroidBuilder(self).module_build(module, modname)

    def ast_from_class(self, klass, modname=None):
        """get astroid for the given class"""
        if modname is None:
            try:
                modname = klass.__module__
            except AttributeError as exc:
                raise AstroidBuildingError(
                    "Unable to get module for class {class_name}.",
                    cls=klass,
                    class_repr=safe_repr(klass),
                    modname=modname,
                ) from exc
        modastroid = self.ast_from_module_name(modname)
        return modastroid.getattr(klass.__name__)[0]  # XXX

    def infer_ast_from_something(self, obj, context=None):
        """infer astroid for the given class"""
        if hasattr(obj, "__class__") and not isinstance(obj, type):
            klass = obj.__class__
        else:
            klass = obj
        try:
            modname = klass.__module__
        except AttributeError as exc:
            raise AstroidBuildingError(
                "Unable to get module for {class_repr}.",
                cls=klass,
                class_repr=safe_repr(klass),
            ) from exc
        except Exception as exc:
            raise AstroidImportError(
                "Unexpected error while retrieving module for {class_repr}:\n"
                "{error}",
                cls=klass,
                class_repr=safe_repr(klass),
            ) from exc
        try:
            name = klass.__name__
        except AttributeError as exc:
            raise AstroidBuildingError(
                "Unable to get name for {class_repr}:\n",
                cls=klass,
                class_repr=safe_repr(klass),
            ) from exc
        except Exception as exc:
            raise AstroidImportError(
                "Unexpected error while retrieving name for {class_repr}:\n" "{error}",
                cls=klass,
                class_repr=safe_repr(klass),
            ) from exc
        # take care, on living object __module__ is regularly wrong :(
        modastroid = self.ast_from_module_name(modname)
        if klass is obj:
            for inferred in modastroid.igetattr(name, context):
                yield inferred
        else:
            for inferred in modastroid.igetattr(name, context):
                yield inferred.instantiate_class()

    def register_failed_import_hook(self, hook):
        """Registers a hook to resolve imports that cannot be found otherwise.

        `hook` must be a function that accepts a single argument `modname` which
        contains the name of the module or package that could not be imported.
        If `hook` can resolve the import, must return a node of type `astroid.Module`,
        otherwise, it must raise `AstroidBuildingError`.
        """
        self._failed_import_hooks.append(hook)

    def cache_module(self, module):
        """Cache a module if no module with the same name is known yet."""
        self.astroid_cache.setdefault(module.name, module)

    def bootstrap(self):
        """Bootstrap the required AST modules needed for the manager to work

        The bootstrap usually involves building the AST for the builtins
        module, which is required by the rest of astroid to work correctly.
        """
        from astroid import raw_building  # pylint: disable=import-outside-toplevel

        raw_building._astroid_bootstrapping()

    def clear_cache(self) -> None:
        """Clear the underlying cache, bootstrap the builtins module and
        re-register transforms."""
        self.astroid_cache.clear()
        AstroidManager.brain["_transform"] = TransformVisitor()
        self.bootstrap()

        # Reload brain plugins. During initialisation this is done in astroid.__init__.py
        for module in BRAIN_MODULES_DIRECTORY.iterdir():
            if module.suffix == ".py":
                module_spec = find_spec(f"astroid.brain.{module.stem}")
                module_object = module_from_spec(module_spec)
                module_spec.loader.exec_module(module_object)
