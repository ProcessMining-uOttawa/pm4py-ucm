# OMG BPMN 2.0 XML Schema

The five `.xsd` files are the normative BPMN 2.0 schema published by the
Object Management Group at <https://www.omg.org/spec/BPMN/20100501/>
(`BPMN20.xsd` imports the other four by relative `schemaLocation`), copied
unmodified on 2026-11-10. They are used only by `tests/test_bpmn_export.py`
to validate the files the BPMN exporter writes; `lxml` (a dev dependency) is
needed to compile them, and the test skips when it is not installed.
