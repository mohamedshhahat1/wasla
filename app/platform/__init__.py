"""The platform layer: reads that deliberately span every workspace.

Everything else in this codebase is built so a query *cannot* cross a tenant
boundary. This package is the exception, and it is a package rather than a few
methods on existing services precisely so the exception is visible: a
cross-tenant read that is not in here is a bug.

Every route these back sits behind the platform-role dependency, which is
separate from workspace roles. Owning a workspace grants nothing here.
"""
