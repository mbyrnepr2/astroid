# Licensed under the LGPL: https://www.gnu.org/licenses/old-licenses/lgpl-2.1.en.html
# For details: https://github.com/PyCQA/astroid/blob/main/LICENSE
# Copyright (c) https://github.com/PyCQA/astroid/blob/main/CONTRIBUTORS.txt

"""This module contains mixin classes for scoped nodes."""

from typing import TYPE_CHECKING, Dict, List, TypeVar

from astroid.filter_statements import _filter_stmts
from astroid.nodes import node_classes, scoped_nodes
from astroid.nodes.scoped_nodes.utils import builtin_lookup

if TYPE_CHECKING:
    from astroid import nodes

_T = TypeVar("_T")


class LocalsDictNodeNG(node_classes.LookupMixIn, node_classes.NodeNG):
    """this class provides locals handling common to Module, FunctionDef
    and ClassDef nodes, including a dict like interface for direct access
    to locals information
    """

    # attributes below are set by the builder module or by raw factories

    locals: Dict[str, List["nodes.NodeNG"]] = {}
    """A map of the name of a local variable to the node defining the local."""

    def qname(self):
        """Get the 'qualified' name of the node.

        For example: module.name, module.class.name ...

        :returns: The qualified name.
        :rtype: str
        """
        # pylint: disable=no-member; github.com/pycqa/astroid/issues/278
        if self.parent is None:
            return self.name
        return f"{self.parent.frame(future=True).qname()}.{self.name}"

    def scope(self: _T) -> _T:
        """The first parent node defining a new scope.

        :returns: The first parent scope node.
        :rtype: Module or FunctionDef or ClassDef or Lambda or GenExpr
        """
        return self

    def _scope_lookup(self, node, name, offset=0):
        """XXX method for interfacing the scope lookup"""
        try:
            stmts = _filter_stmts(node, self.locals[name], self, offset)
        except KeyError:
            stmts = ()
        if stmts:
            return self, stmts

        # Handle nested scopes: since class names do not extend to nested
        # scopes (e.g., methods), we find the next enclosing non-class scope
        pscope = self.parent and self.parent.scope()
        while pscope is not None:
            if not isinstance(pscope, scoped_nodes.ClassDef):
                return pscope.scope_lookup(node, name)
            pscope = pscope.parent and pscope.parent.scope()

        # self is at the top level of a module, or is enclosed only by ClassDefs
        return builtin_lookup(name)

    def set_local(self, name, stmt):
        """Define that the given name is declared in the given statement node.

        .. seealso:: :meth:`scope`

        :param name: The name that is being defined.
        :type name: str

        :param stmt: The statement that defines the given name.
        :type stmt: NodeNG
        """
        # assert not stmt in self.locals.get(name, ()), (self, stmt)
        self.locals.setdefault(name, []).append(stmt)

    __setitem__ = set_local

    def _append_node(self, child):
        """append a child, linking it in the tree"""
        # pylint: disable=no-member; depending by the class
        # which uses the current class as a mixin or base class.
        # It's rewritten in 2.0, so it makes no sense for now
        # to spend development time on it.
        self.body.append(child)
        child.parent = self

    def add_local_node(self, child_node, name=None):
        """Append a child that should alter the locals of this scope node.

        :param child_node: The child node that will alter locals.
        :type child_node: NodeNG

        :param name: The name of the local that will be altered by
            the given child node.
        :type name: str or None
        """
        if name != "__class__":
            # add __class__ node as a child will cause infinite recursion later!
            self._append_node(child_node)
        self.set_local(name or child_node.name, child_node)

    def __getitem__(self, item: str) -> "nodes.NodeNG":
        """The first node the defines the given local.

        :param item: The name of the locally defined object.

        :raises KeyError: If the name is not defined.
        """
        return self.locals[item][0]

    def __iter__(self):
        """Iterate over the names of locals defined in this scoped node.

        :returns: The names of the defined locals.
        :rtype: iterable(str)
        """
        return iter(self.keys())

    def keys(self):
        """The names of locals defined in this scoped node.

        :returns: The names of the defined locals.
        :rtype: list(str)
        """
        return list(self.locals.keys())

    def values(self):
        """The nodes that define the locals in this scoped node.

        :returns: The nodes that define locals.
        :rtype: list(NodeNG)
        """
        # pylint: disable=consider-using-dict-items
        # It look like this class override items/keys/values,
        # probably not worth the headache
        return [self[key] for key in self.keys()]

    def items(self):
        """Get the names of the locals and the node that defines the local.

        :returns: The names of locals and their associated node.
        :rtype: list(tuple(str, NodeNG))
        """
        return list(zip(self.keys(), self.values()))

    def __contains__(self, name):
        """Check if a local is defined in this scope.

        :param name: The name of the local to check for.
        :type name: str

        :returns: True if this node has a local of the given name,
            False otherwise.
        :rtype: bool
        """
        return name in self.locals


class ComprehensionScope(LocalsDictNodeNG):
    """Scoping for different types of comprehensions."""

    scope_lookup = LocalsDictNodeNG._scope_lookup

    generators: List["nodes.Comprehension"]
    """The generators that are looped through."""
