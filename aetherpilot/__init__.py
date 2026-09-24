"""AetherPilot's public, vendor-independent entry points."""

__version__ = "0.1.0"


def backend_status(config=None):
    """Describe the optional native backend without importing vendor code."""
    from .backend import backend_status as _backend_status

    return _backend_status(config)


def get_backend(config=None):
    """Return the configured native marker or offline stub facade."""
    from .backend import get_backend as _get_backend

    return _get_backend(config)


def run(plan, *, config=None):
    from .harness import Harness

    return Harness(config).run(plan)


def recover_change(publication_operation_id, *, domain="schematic", approved=False, config=None):
    from .harness import Harness

    return Harness(config).recover_change(publication_operation_id, domain=domain, approved=approved)


def run_layout(plan, *, config=None):
    from .harness import Harness

    return Harness(config).run_layout(plan)


def run_symbol(plan, *, config=None):
    from .harness import Harness

    return Harness(config).run_symbol(plan)


def append_symbol(plan, *, base_sha256, config=None):
    from .harness import Harness

    return Harness(config).append_symbol(plan, base_sha256=base_sha256)


def append_layout(plan, *, base_sha256, config=None):
    from .harness import Harness

    return Harness(config).append_layout(plan, base_sha256=base_sha256)


def generate_symbol(target, *, schematic_view="schematic", symbol_view="symbol", sides=None,
                    symbol_shape="transmission", pin_space=0.125, pin_length=0.25,
                    pin_shape="round", pin_origin="topLeft", overwrite=False, sort_pins=None, config=None):
    from .harness import Harness

    return Harness(config).generate_symbol(target, schematic_view=schematic_view, symbol_view=symbol_view,
                                           sides=sides, symbol_shape=symbol_shape, pin_space=pin_space,
                                           pin_length=pin_length, pin_shape=pin_shape, pin_origin=pin_origin,
                                           overwrite=overwrite, sort_pins=sort_pins)


def read_symbol(target, *, config=None):
    from .harness import Harness

    return Harness(config).read_symbol(target)


def read_schematic(target, *, recursive=True, max_depth=20, include_geometry=True,
                   include_properties=True, configuration=None, config=None):
    """Read a schematic using the guarded native Aether readback contract."""
    from .harness import Harness

    return Harness(config).read_schematic(
        target, recursive=recursive, max_depth=max_depth,
        include_geometry=include_geometry, include_properties=include_properties,
        configuration=configuration,
    )


def review(readback, *, op=None, result=None, intent=None, source=None, netlist=None, config=None):
    """Run the bounded read-only Design Reviewer on saved facts."""
    from .harness import Harness

    return Harness(config).review(readback, op=op, result=result, intent=intent, source=source, netlist=netlist)


def review_files(readback_path, *, op_path=None, intent_path=None, netlist_path=None, config=None):
    """Review immutable readback/OP files without modifying Aether state."""
    from .harness import Harness

    return Harness(config).review_files(readback_path, op_path=op_path, intent_path=intent_path, netlist_path=netlist_path)


def natural_language_plan(description, *, target, grid=0.0625, constraints=None, routing=None, config=None):
    """Compile a bounded natural-language circuit description to schematic IR."""
    from .harness import Harness

    return Harness(config).natural_language_plan(description, target=target, grid=grid,
                                                  constraints=constraints, routing=routing)


def natural_language_apply(description, *, target, grid=0.0625, constraints=None, routing=None, config=None):
    """Compile a bounded description and create a new native schematic."""
    from .harness import Harness

    return Harness(config).natural_language_apply(description, target=target, grid=grid,
                                                   constraints=constraints, routing=routing)


def maestro_action(target, action, *, runtime_options=None, attached_api=None, config=None, **values):
    from .harness import Harness

    return Harness(config).maestro_action(target, action, runtime_options=runtime_options, attached_api=attached_api, **values)


def maestro_read(target, *, options=None, session_id="", attached_api=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_read(target, options=options, session_id=session_id, attached_api=attached_api)


def maestro_open_session(target, *, options=None, persistent=False, attached_api=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_open_session(target, options=options, persistent=persistent, attached_api=attached_api)


def maestro_close_session(target, *, options=None, session_id="", attached_api=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_close_session(target, options=options, session_id=session_id, attached_api=attached_api)


def maestro_find_open_session(target=None, *, options=None, session_id="", attached_api=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_find_open_session(target, options=options, session_id=session_id, attached_api=attached_api)


def maestro_capabilities(target=None, *, options=None, attached_api=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_capabilities(target, options=options, attached_api=attached_api)


def maestro_migrate_setup(source_target, target, *, test="imported", options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_migrate_setup(source_target, target, test=test, options=options)


def maestro_run(target, *, test="", output_dir=None, options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_run(target, test=test, output_dir=output_dir, options=options)


def maestro_create_netlist(target, *, test="", corner="", output_dir=None, options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_create_netlist(target, test=test, corner=corner, output_dir=output_dir, options=options)


def maestro_read_results(target, *, history="", test="", options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_read_results(target, history=history, test=test, options=options)


def maestro_export_output_view(target, *, output, table_view="detail", history="", test="", options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_export_output_view(target, output=output, table_view=table_view, history=history, test=test, options=options)


def maestro_write_script(target, *, output, options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_write_script(target, output=output, options=options)


def maestro_recover(target, *, options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_recover(target, options=options)


def maestro_export_waveform(target, *, analysis, expression, output, history="", test="", corner="", options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_export_waveform(target, analysis=analysis, expression=expression, output=output,
                                                   history=history, test=test, corner=corner, options=options)


def maestro_snapshot(target, *, options=None, config=None):
    from .harness import Harness

    return Harness(config).maestro_snapshot(target, options=options)


def library_get(name, *, config=None):
    from .harness import Harness

    return Harness(config).library_get(name)


def library_create(name, *, path=None, technology_library=None, config=None):
    from .harness import Harness

    return Harness(config).library_create(name, path=path, technology_library=technology_library)


def library_delete(name, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).library_delete(name, approved=approved)


def library_rename(name, new_name, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).library_rename(name, new_name, approved=approved)


def library_set_technology(name, technology_library, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).library_set_technology(name, technology_library, approved=approved)


def category_list(library, *, config=None):
    from .harness import Harness

    return Harness(config).categories(library)


def category_create(library, category, *, cells=None, approved=False, config=None):
    from .harness import Harness

    return Harness(config).category_create(library, category, cells=cells, approved=approved)


def category_delete(library, category, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).category_delete(library, category, approved=approved)


def category_cells(library, category, *, config=None):
    from .harness import Harness

    return Harness(config).category_cells(library, category)


def category_add_cell(library, category, cell, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).category_add_cell(library, category, cell, approved=approved)


def category_remove_cell(library, category, cell, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).category_remove_cell(library, category, cell, approved=approved)


def category_rename(library, category, new_name, *, approved=False, config=None):
    from .harness import Harness

    return Harness(config).category_rename(library, category, new_name, approved=approved)


def export_netlist(target, *, output=None, device_models=None, recursive=False, netlist_format="flat", config=None):
    from .harness import Harness

    return Harness(config).export_netlist(target, output=output, device_models=device_models, recursive=recursive, netlist_format=netlist_format)


def export_hierarchical_netlist(target, *, output=None, netlist_format="spice", config=None):
    from .harness import Harness

    return Harness(config).export_hierarchical_netlist(target, output=output, netlist_format=netlist_format)


def import_netlist(source, target, *, require_metadata=False, recursive=False, netlist_format="auto", materialize=False, config=None):
    from .harness import Harness

    return Harness(config).import_netlist(source, target, require_metadata=require_metadata, recursive=recursive, netlist_format=netlist_format, materialize=materialize)


def import_hierarchical_netlist(source, target, *, netlist_format="auto", materialize=False, config=None):
    from .harness import Harness

    return Harness(config).import_hierarchical_netlist(source, target, netlist_format=netlist_format, materialize=materialize)


def migrate_netlist(source, target, manifest, *, approved=False, materialize=False, config=None):
    """Migrate a front-end netlist with an explicit PDK mapping manifest."""
    from .harness import Harness

    return Harness(config).migrate_netlist(source, target, manifest, approved=approved, materialize=materialize)


def export_native_netlist(target, *, output_dir=None, model_libraries=None, temperature=27, netlist_format="hspice", config=None):
    from .harness import Harness

    return Harness(config).export_native_netlist(target, output_dir=output_dir, model_libraries=model_libraries, temperature=temperature, netlist_format=netlist_format)
