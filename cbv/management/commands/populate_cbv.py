import importlib
import inspect
import sys
from collections import defaultdict
from typing import Iterator

import django
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand
from django.utils.functional import Promise

from cbv import models
from cbv.importer.dataclasses import Klass, KlassAttribute, Method, Module


class LazyAttribute:
    functions = {
        "gettext": "gettext_lazy",
        "reverse": "reverse_lazy",
        "ugettext": "ugettext_lazy",
    }

    def __init__(self, promise):
        func, self.args, self.kwargs, _ = promise.__reduce__()[1]
        try:
            self.lazy_func = self.functions[func.__name__]
        except KeyError:
            msg = f"'{func.__name__}' not in known lazily called functions"
            raise ImproperlyConfigured(msg)

    def __repr__(self):
        arguments = []
        for arg in self.args:
            if isinstance(arg, str):
                arguments.append(f"'{arg}'")
            else:
                arguments.append(arg)
        for key, value in self.kwargs:
            if isinstance(key, str):
                key = f"'{key}'"
            if isinstance(value, str):
                value = f"'{value}'"
            arguments.append(f"{key}: {value}")
        func = self.lazy_func
        arguments = ", ".join(arguments)
        return f"{func}({arguments})"


BANNED_ATTR_NAMES = (
    "__all__",
    "__builtins__",
    "__class__",
    "__dict__",
    "__doc__",
    "__file__",
    "__module__",
    "__name__",
    "__package__",
    "__path__",
    "__spec__",
    "__weakref__",
)


class Command(BaseCommand):
    args = ""
    help = "Wipes and populates the CBV inspection models."

    def handle(self, *args, **options):
        module_paths = settings.CBV_SOURCES.keys()
        members = InspectCodeImporter().process_modules(module_paths=module_paths)

        DBStorage().import_project_version(
            members=members,
            project_name="Django",
            project_version=django.get_version(),
        )


class DBStorage:
    def import_project_version(
        self, *, members: Iterator, project_name: str, project_version: str
    ):
        self._wipe_clashing_data(
            project_name=project_name, project_version=project_version
        )

        # Setup Project
        project_version = models.ProjectVersion.objects.create(
            project=models.Project.objects.get_or_create(name=project_name)[0],
            version_number=project_version,
        )

        klasses = []
        attributes: defaultdict[tuple[str, str], list[tuple[str, int]]] = defaultdict(
            list
        )
        klass_models: dict[str, models.Klass] = {}
        module_models: dict[str, models.Module] = {}
        method_models: list[models.Method] = []

        for member in members:
            if isinstance(member, Module):
                module_model = models.Module.objects.create(
                    project_version=project_version,
                    name=member.name,
                    docstring=member.docstring,
                    filename=member.filename,
                )
                module_models[member.name] = module_model
            elif isinstance(member, KlassAttribute):
                attributes[(member.name, member.value)] += [
                    (member.klass_path, member.line_number)
                ]
            elif isinstance(member, Method):
                method = models.Method(
                    klass=klass_models[member.klass_path],
                    name=member.name,
                    docstring=member.docstring,
                    code=member.code,
                    kwargs=member.kwargs,
                    line_number=member.line_number,
                )
                method_models.append(method)
            elif isinstance(member, Klass):
                klass_model = models.Klass.objects.create(
                    module=module_models[member.module],
                    name=member.name,
                    docstring=member.docstring,
                    line_number=member.line_number,
                    import_path=member.best_import_path,
                )
                klass_models[member.path] = klass_model
                klasses.append(member)

        models.Method.objects.bulk_create(method_models)
        create_inheritance(klasses, klass_models)
        create_attributes(attributes, klass_models)

    def _wipe_clashing_data(self, *, project_name: str, project_version: str) -> None:
        """Delete existing data in the DB to make way for this new import."""
        # We don't really care about deleting the ProjectVersion here in particular.
        # In fact, we'll re-create it later.
        # Instead, we're using the cascading delete to remove all the dependent objects.
        models.ProjectVersion.objects.filter(
            project__name__iexact=project_name,
            version_number=project_version,
        ).delete()
        models.Inheritance.objects.filter(
            parent__module__project_version__project__name__iexact=project_name,
            parent__module__project_version__version_number=project_version,
        ).delete()


class InspectCodeImporter:
    """Generates code structure classes by using the inspect module."""

    def process_modules(self, *, module_paths):
        modules = []
        for module_path in module_paths:
            try:
                modules.append(importlib.import_module(module_path))
            except ImportError:
                pass

        for module in modules:
            module_name = module.__name__

            yield from self._process_member(
                member=module,
                member_name=module_name,
                root_module_name=module_name,
                parent=None,
            )

    def _handle_module(self, module, root_module_name):
        module_name = module.__name__
        # Only traverse under hierarchy
        if not module_name.startswith(root_module_name):
            return None

        filename = get_filename(module)
        # Create Module object
        yield Module(
            name=module_name,
            docstring=get_docstring(module),
            filename=filename,
        )
        # Go through members
        yield from self._process_submembers(
            root_module_name=root_module_name, parent=module
        )

    def _get_best_import_path_for_class(self, klass) -> str:
        module_path = best_path = klass.__module__

        while module_path := module_path.rpartition(".")[0]:
            module = importlib.import_module(module_path)
            if getattr(module, klass.__name__, None) == klass:
                best_path = module_path
        return best_path

    def _handle_class_on_module(self, member, parent, root_module_name):
        if not member.__module__.startswith(parent.__name__):
            return None

        if inspect.getsourcefile(member) != inspect.getsourcefile(parent):
            return None

        yield Klass(
            name=member.__name__,
            module=member.__module__,
            docstring=get_docstring(member),
            line_number=get_line_number(member),
            path=_full_path(member),
            bases=[_full_path(k) for k in member.__bases__],
            best_import_path=self._get_best_import_path_for_class(member),
        )
        # Go through members
        yield from self._process_submembers(
            root_module_name=root_module_name, parent=member
        )

    def _handle_function_or_method(self, member, member_name, parent):
        # Decoration
        while getattr(member, "__wrapped__", None):
            member = member.__wrapped__

        # Checks
        if not ok_to_add_method(member, parent):
            return

        code, arguments, start_line = get_code(member)

        yield Method(
            name=member_name,
            docstring=get_docstring(member),
            code=code,
            kwargs=arguments[1:-1],
            line_number=start_line,
            klass_path=_full_path(parent),
        )

    def _handle_class_attribute(self, member, member_name, parent):
        # Replace lazy function call with an object representing it
        if isinstance(member, Promise):
            member = LazyAttribute(member)

        if not ok_to_add_attribute(member, member_name, parent):
            return

        yield KlassAttribute(
            name=member_name,
            value=get_value(member),
            line_number=get_line_number(member),
            klass_path=_full_path(parent),
        )

    def _process_member(self, *, member, member_name, root_module_name, parent):
        # BUILTIN
        if inspect.isbuiltin(member):
            pass

        # MODULE
        elif inspect.ismodule(member):
            yield from self._handle_module(member, root_module_name)

        # CLASS
        elif inspect.isclass(member) and inspect.ismodule(parent):
            yield from self._handle_class_on_module(member, parent, root_module_name)

        # METHOD
        elif inspect.ismethod(member) or inspect.isfunction(member):
            yield from self._handle_function_or_method(member, member_name, parent)

        # (Class) ATTRIBUTE
        elif inspect.isclass(parent):
            yield from self._handle_class_attribute(member, member_name, parent)

    def _process_submembers(self, *, parent, root_module_name):
        for submember_name, submember_type in inspect.getmembers(parent):
            yield from self._process_member(
                member=submember_type,
                member_name=submember_name,
                root_module_name=root_module_name,
                parent=parent,
            )


def _full_path(klass: type) -> str:
    return f"{klass.__module__}.{klass.__name__}"


def create_attributes(attributes, klass_lookup):
    # Go over each name/value pair to create KlassAttributes
    attribute_models = []
    for (name, value), klasses in attributes.items():

        # Find all the descendants of each Klass.
        descendants = set()
        for klass_path, start_line in klasses:
            klass = klass_lookup[klass_path]
            for child in klass.get_all_children():
                descendants.add(child)

        # By removing descendants from klasses, we leave behind the
        # klass(s) where the value was defined.
        remaining_klasses = [
            k_and_l
            for k_and_l in klasses
            if klass_lookup[k_and_l[0]] not in descendants
        ]

        # Now we can create the KlassAttributes
        for klass_path, line in remaining_klasses:
            klass = klass_lookup[klass_path]
            attribute_models.append(
                models.KlassAttribute(
                    klass=klass, line_number=line, name=name, value=value
                )
            )

    models.KlassAttribute.objects.bulk_create(attribute_models)


def create_inheritance(klasses, klass_lookup):
    inheritance_models = []
    for klass_data in klasses:
        direct_ancestors = klass_data.bases
        for i, ancestor in enumerate(direct_ancestors):
            if ancestor in klass_lookup:
                inheritance_models.append(
                    models.Inheritance(
                        parent=klass_lookup[ancestor],
                        child=klass_lookup[klass_data.path],
                        order=i,
                    )
                )
    models.Inheritance.objects.bulk_create(inheritance_models)


def get_code(member):
    # Strip unneeded whitespace from beginning of code lines
    lines, start_line = inspect.getsourcelines(member)
    whitespace = len(lines[0]) - len(lines[0].lstrip())
    for i, line in enumerate(lines):
        lines[i] = line[whitespace:]

    # Join code lines into one string
    code = "".join(lines)

    # Get the method arguments
    arguments = inspect.formatargspec(*inspect.getfullargspec(member))

    return code, arguments, start_line


def get_docstring(member):
    return inspect.getdoc(member) or ""


def get_filename(member):
    # Get full file name
    filename = inspect.getfile(member)

    # Find the system path it's in
    sys_folder = max((p for p in sys.path if p in filename), key=len)

    # Get the part of the file name after the folder on the system path.
    filename = filename[len(sys_folder) :]

    # Replace `.pyc` file extensions with `.py`
    if filename[-4:] == ".pyc":
        filename = filename[:-1]
    return filename


def get_line_number(member):
    try:
        return inspect.getsourcelines(member)[1]
    except TypeError:
        return -1


def get_value(member):
    return f"'{member}'" if isinstance(member, str) else str(member)


def ok_to_add_attribute(member, member_name, parent):
    if inspect.isclass(parent) and member in object.__dict__.values():
        return False

    if member_name in BANNED_ATTR_NAMES:
        return False
    return True


def ok_to_add_method(member, parent):
    if inspect.getsourcefile(member) != inspect.getsourcefile(parent):
        return False

    if not inspect.isclass(parent):
        return False

    # Use line inspection to work out whether the method is defined on this
    # klass. Possibly not the best way, but I can't think of another atm.
    lines, start_line = inspect.getsourcelines(member)
    parent_lines, parent_start_line = inspect.getsourcelines(parent)
    if start_line < parent_start_line or start_line > parent_start_line + len(
        parent_lines
    ):
        return False
    return True
