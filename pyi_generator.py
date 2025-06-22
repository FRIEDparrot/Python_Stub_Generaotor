import inspect
import os
import sys
from pathlib import Path
from typing import Set, Dict, Any, Optional
import importlib
import argparse


class ModuleStubGenerator:
    def __init__(self, output_dir: str = "stubs", remove_if_exists: bool = False):
        self.output_dir = Path(output_dir)
        self.processed_modules: Set[str] = set()
        self.processed_classes: Set[str] = set()
        self.processed_class_objects: Set[int] = set()  # Track by object id
        self.class_name_mapping: Dict[int, str] = {}  # Map object id to chosen name
        self.module_imports: Dict[str, Set[str]] = {}
        self.remove_if_exists: bool = remove_if_exists

    def _sanitize_name(self, name: str) -> str:
        """Sanitize names to be valid Python identifiers"""
        if not name.isidentifier():
            return f"_{name}"
        return name

    def _get_type_annotation(self, obj: Any) -> str:
        """Get type annotation for an object"""
        try:
            if hasattr(obj, '__annotations__'):
                return str(obj.__annotations__.get('return', 'Any'))
            elif isinstance(obj, (int, float, str, bool)):
                return type(obj).__name__
            else:
                return 'Any'
        except:
            return 'Any'

    def _is_internal_module(self, module_name: str) -> bool:
        """Check if a module is internal/builtin and should be skipped"""
        internal_prefixes = [
            'builtins', '_', 'sys', 'os', 'io', 'collections',
            'typing', 'abc', 'enum', 'functools', 'itertools',
            'operator', 'weakref', 'threading', 'multiprocessing'
        ]
        return any(module_name.startswith(prefix) for prefix in internal_prefixes)

    def dump_class_stub(self, cls, module_path: str) -> str:
        """Generate stub for a single class"""
        class_name = self._sanitize_name(cls.__name__)
        stub_content = []

        # Class definition with inheritance
        bases = []
        if hasattr(cls, '__bases__') and cls.__bases__:
            for base in cls.__bases__:
                if base.__name__ != 'object':
                    bases.append(base.__name__)

        if bases:
            stub_content.append(f"class {class_name}({', '.join(bases)}):")
        else:
            stub_content.append(f"class {class_name}:")

        # Class docstring
        if hasattr(cls, '__doc__') and cls.__doc__:
            doc = str(cls.__doc__).replace('"""', '\\"\\"\\"').strip()
            if doc:
                stub_content.append(f'    """{doc[:200]}{"..." if len(doc) > 200 else ""}"""')

        # Collect class members
        methods = []
        properties = []
        class_vars = []
        has_members = False

        for name in sorted(dir(cls)):
            if name.startswith("_") and name not in ["__init__", "__new__", "__call__", "__str__", "__repr__"]:
                continue

            try:
                attr = getattr(cls, name)
            except Exception:
                continue

            sanitized_name = self._sanitize_name(name)
            has_members = True

            if callable(attr):
                try:
                    sig = inspect.signature(attr)
                    methods.append(f"    def {sanitized_name}{sig}: ...")
                except Exception:
                    if name in ["__init__", "__new__"]:
                        methods.append(f"    def {sanitized_name}(self, *args, **kwargs): ...")
                    elif inspect.ismethod(attr) or hasattr(attr, '__self__'):
                        methods.append(f"    def {sanitized_name}(self, *args, **kwargs): ...")
                    else:
                        methods.append(f"    @staticmethod\n    def {sanitized_name}(*args, **kwargs): ...")
            elif isinstance(attr, property):
                properties.append(f"    {sanitized_name}: Any")
            else:
                type_hint = self._get_type_annotation(attr)
                class_vars.append(f"    {sanitized_name}: {type_hint}")

        # Add members to stub
        if not has_members:
            stub_content.append("    pass")
        else:
            stub_content.extend(class_vars)
            stub_content.extend(properties)
            stub_content.extend(methods)

        return "\n".join(stub_content) + "\n\n"

    def dump_function_stub(self, func) -> str:
        """Generate stub for a function"""
        func_name = self._sanitize_name(func.__name__)
        try:
            sig = inspect.signature(func)
            return f"def {func_name}{sig}: ...\n"
        except Exception:
            return f"def {func_name}(*args, **kwargs): ...\n"

    def get_module_members(self, module) -> Dict[str, Any]:
        """Get all members of a module, categorized by type"""
        members = {
            'classes': {},
            'functions': {},
            'submodules': {},
            'variables': {}
        }

        module_name = getattr(module, '__name__', 'unknown')
        class_objects_in_module = {}  # Track class objects by id to detect duplicates

        for name in dir(module):
            if name.startswith("_"):
                continue

            try:
                attr = getattr(module, name)
            except Exception as e:
                print(f"Warning: Could not access {module_name}.{name}: {e}")
                continue

            if inspect.isclass(attr):
                # Only include classes defined in this module or without a module
                attr_module = getattr(attr, '__module__', None)
                if attr_module is None or attr_module == module_name or attr_module.startswith(module_name + '.'):

                    # Check for duplicate class objects (same class with different names)
                    class_id = id(attr)
                    if class_id in class_objects_in_module:
                        existing_name = class_objects_in_module[class_id]
                        print(f"  Skipping duplicate class: {name} (same as {existing_name})")
                        continue

                    # Choose the best name (prefer class.__name__ over alias)
                    class_actual_name = getattr(attr, '__name__', name)
                    chosen_name = class_actual_name if class_actual_name == name else name

                    class_objects_in_module[class_id] = chosen_name
                    self.class_name_mapping[class_id] = chosen_name
                    members['classes'][chosen_name] = attr

            elif inspect.ismodule(attr):
                attr_module_name = getattr(attr, '__name__', name)
                # Only include submodules that are actually submodules of current module
                if (attr_module_name != module_name and
                        attr_module_name.startswith(module_name + '.') and
                        not self._is_internal_module(attr_module_name)):
                    members['submodules'][name] = attr

            elif callable(attr) and not inspect.isclass(attr):
                # Only include functions defined in this module
                attr_module = getattr(attr, '__module__', None)
                if attr_module is None or attr_module == module_name:
                    members['functions'][name] = attr

            else:
                # Module-level variables
                members['variables'][name] = attr

        return members

    def create_module_structure(self, module, base_path: Path, module_name: str = None):
        """Recursively create module structure and generate stubs"""
        if module_name is None:
            module_name = getattr(module, '__name__', 'unknown')

        # Skip if already processed
        full_module_key = f"{base_path}/{module_name}"
        if full_module_key in self.processed_modules:
            return

        print(f"Processing module: {module_name}")
        self.processed_modules.add(full_module_key)

        # Create directory for this module
        safe_module_name = self._sanitize_name(module_name.split('.')[-1])
        module_dir = base_path / safe_module_name
        module_dir.mkdir(parents=True, exist_ok=True)

        # Get all module members
        members = self.get_module_members(module)

        # Content for __init__.pyi
        init_content = ["from typing import Any\n"]

        # Process classes - add them directly to __init__.pyi instead of separate files
        for class_name, cls in members['classes'].items():
            class_id = id(cls)
            class_full_name = f"{module_name}.{class_name}"

            # Check if this class object was already processed globally
            if class_id in self.processed_class_objects:
                existing_name = self.class_name_mapping.get(class_id, class_name)
                print(f"  Skipping already processed class: {class_name} (processed as {existing_name})")
                # Add alias if different name
                safe_class_name = self._sanitize_name(class_name)
                safe_existing_name = self._sanitize_name(existing_name)
                if safe_class_name != safe_existing_name:
                    init_content.append(f"{safe_class_name} = {safe_existing_name}")
                continue

            # Mark this class object as processed
            self.processed_class_objects.add(class_id)
            self.processed_classes.add(class_full_name)

            print(f"  Adding class to __init__.pyi: {class_name}")

            # Add class definition directly to __init__.pyi
            init_content.append(self.dump_class_stub(cls, module_name))

        # Process functions
        for func_name, func in members['functions'].items():
            init_content.append(self.dump_function_stub(func))

        # Process variables
        for var_name, var in members['variables'].items():
            type_hint = self._get_type_annotation(var)
            safe_var_name = self._sanitize_name(var_name)
            init_content.append(f"{safe_var_name}: {type_hint}")

        # Process submodules
        for submodule_name, submodule in members['submodules'].items():
            try:
                print(f"  Found submodule: {submodule_name}")
                # Recursively process submodule
                self.create_module_structure(submodule, module_dir, getattr(submodule, '__name__', submodule_name))

                # Add import to __init__.pyi
                safe_submodule_name = self._sanitize_name(submodule_name)
                init_content.append(f"from . import {safe_submodule_name}")

            except Exception as e:
                print(f"Warning: Could not process submodule {submodule_name}: {e}")

        # Write __init__.pyi
        init_file = module_dir / "__init__.pyi"
        print(f"  Creating init file: {init_file}")

        with open(init_file, 'w', encoding='utf-8') as f:
            content = "\n".join(init_content)
            if content.strip():
                f.write(content)
                if not content.endswith('\n'):
                    f.write('\n')
            else:
                f.write("from typing import Any\n")

    def dump_module(self, module):
        """Main entry point to dump a module"""
        module_name = getattr(module, '__name__', str(module))
        print(f"Starting stub generation for module: {module_name}")

        # Clean output directory
        if self.output_dir.exists() and self.remove_if_exists:
            import shutil
            shutil.rmtree(self.output_dir)  # Remove existing directory

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Generate stubs
        try:
            self.create_module_structure(module, self.output_dir)
            print(f"\nStub generation completed!")
            print(f"Generated stubs in: {self.output_dir.absolute()}")
            print(f"Processed {len(self.processed_modules)} modules")
            print(f"Processed {len(self.processed_classes)} classes")

            # List generated files
            print("\nGenerated files:")
            for root, dirs, files in os.walk(self.output_dir):
                for file in sorted(files):
                    if file.endswith('.pyi'):
                        rel_path = os.path.relpath(os.path.join(root, file), self.output_dir)
                        print(f"  {rel_path}")

        except Exception as e:
            print(f"Error during stub generation: {e}")
            import traceback
            traceback.print_exc()


def dump_module(module, output_dir: str = "stubs", remove_if_exists: bool = False):
    """Convenience function to dump a module"""
    generator = ModuleStubGenerator(output_dir, remove_if_exists=remove_if_exists)
    generator.dump_module(module)

def main():
    parser = argparse.ArgumentParser(description='Generate stubs for a Python Package')
    parser.add_argument('package_name', type=str, help='Name of the package to generate stubs for')
    parser.add_argument('output_dir', type=str, default="stubs", nargs='?', help='Directory to write generated stubs in (default: stubs)')
    parser.add_argument('--remove_if_exists', default=False, action='store_true', help='Remove existing stubs directory before generating stubs')
    args = parser.parse_args()

    package_name = ""
    try:
        package_name = args.package_name
        # Dynamically import the package
        package = importlib.import_module(package_name)
        # Initialize package
        try:
            package.init()
        except AttributeError:
            print(f"Error: The package '{package_name}' does not have an 'init' method.")
            pass
        print("Generating stubs for package " + package_name)
        dump_module(package, "stubs")  # Generate stubs

    # parse error
    except SyntaxError as e:
        print(e)

    except ImportError as e:
        print(f"package not found: {e}")

    except Exception as e:
        print(f"package generation failed: {e}")

    print("Stub generation completed!")

if __name__ == "__main__":
    main()
