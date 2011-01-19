import os
import types

# We are aware of ``zope.interface.Interface``: if zope.interfaces is available
# we check interfaces implemented on the plumbing plugins and will make the
# plumbing implement them, too.
try:
    from zope.interface import classImplements
    from zope.interface import implementedBy
    ZOPE_INTERFACE_AVAILABLE = True
except ImportError:
    ZOPE_INTERFACE_AVAILABLE = False


class PlumbingCollision(RuntimeError):
    pass


class extensiondecor(object):
    """A marker for attributes to be copied to the plumbing class' __dict__

    Markers will be removed from the attribute so it is possible to::

        >>> class Plugin(object):
        ...     foo = default(1)
        ...     bar = default(foo)

    """
    def __init__(self, attr):
        self.attr = self.unwrap(attr)

    def unwrap(self, attr):
        if not isinstance(attr, extensiondecor):
            return attr
        return self.unwrap(attr.attr)


class default(extensiondecor):
    """Provide a default value for something

    The first plugin with a default value wins the first round: its value is
    set on the plumbing as if it was declared there.

    Attributes set with the ``extend`` decorator overrule ``default``
    attributes (see ``extend`` decorator).
    """


class extend(extensiondecor):
    """Declare an attribute on the plumning as if it was defined on it.

    Attribute set with the ``extend`` decorator overrule ``default``
    attributes. Two ``extend`` attributes in a chain raise a PlumbingCollision.
    """


def plumb(attr):
    if type(attr) is property:
        return plumbproperty(attr.fget, attr.fset, attr.fdel)
    elif type(attr) is types.FunctionType:
        return plumbmethod(attr)
    else:
        raise TypeError("instance of %s cannot be plumbed" % (type(attr),))


class plumbmethod(classmethod):
    """Mark a method to be used in a plumbing chain.

    The signature of the method is:
    ``def foo(plb, _next, self, *args, **kw)``

    A plumbing method is a classmethod bound to the plugin class defining it
    (``plb``), as second argument it receives the next plumbing method
    (``_next``) and the third argument (``self``) is a plumbing instance, that
    for normal methods would be the first argument.

    In order to plumb a method there needs to be a non-plumbing method behind
    it provided by: a plumbing plugin via ``extend`` or ``default`` later in
    the pipeline, the class itself or one of its base classes.
    """

class plumbproperty(property):
    """mark a property as a plumbing property

    Signature of getter, setter and deleter:
    - ``def getter(plb, _next, self)``
    - ``def setter(plb, _next, self, val)``
    - ``def deleter(plb, _next, self)``

    In order to use a plumbing property, there needs to be a non-plumbing
    property on the class, by the time the end-points for getter, setter and
    deleter are looked up, provided by a plumbing ``extend`` or ``default``,
    the plumbing class itself, or one of its base classes.
    """

def merge_doc(first, *args):
    if not args:
        return first.__doc__

    rest_doc = merge_doc(*args)
    if rest_doc is None:
        return first.__doc__

    if first.__doc__ is None:
        return rest_doc

    return os.linesep.join((first.__doc__, rest_doc))


def entrance(name, pipe):
    """Create an entrance to a pipeline.

    recursively:
    - pop first method from pipeline
    - create entrance to the rest of the pipe as _next
    - wrap method passing it _next and return it, if not last method
    - return last method as is, if last method
    """
    # If only one element is left in the pipe, it is a normal method that does
    # not expect a ``_next`` parameter.
    if len(pipe) is 1:
        return pipe[0]

    # XXX: traceback supplement for pdb, probably more than just name is needed

    plumbattr = pipe.pop(0)
    _next = entrance(name, pipe)
    if isinstance(plumbattr, plumbproperty):
        # XXX: support plb for fget/fset/fdel if they are defined on the plugin
        # class and not just exist in the property
        def get_entrance(self):
            return plumbattr.fget(_next.fget, self)
        def set_entrance(self, val):
            return plumbattr.fset(_next.fset, self, val)
        def del_entrance(self):
            return plumbattr.fdel(_next.fdel, self)
        _entrance = property(get_entrance, set_entrance, del_entrance)
    elif isinstance(plumbattr, types.MethodType):
        def _entrance(self, *args, **kw):
            return plumbattr(_next, self, *args, **kw)
        _entrance.__doc__ = merge_doc(_next, plumbattr)
    else:
        raise TypeError("Cannot plumb instance of %s: %s." % \
                (type(plumbattr), plumbattr))
    return _entrance


class CLOSED(object):
    """used for marking a pipeline as closed
    """


class Plumber(type):
    """Metaclass for plumbing creation

    First the normal new-style metaclass ``type()`` is called to construct the
    class with ``name``, ``bases``, ``dct``.

    Then, if the class declares a ``__pipeline__`` attribute, the plumber
    will create a plumbing system accordingly. Attributes declared with
    ``default``, ``extend`` and ``plumb`` will be used in the plumbing.
    """
    def __init__(cls, name, bases, dct):
        super(Plumber, cls).__init__(name, bases, dct)
        # The metaclass is inherited.
        # The plumber will only get active if the class it produces defines a
        # __pipeline__.
        if cls.__dict__.get('__pipeline__') is None:
            return
        if type(cls.__pipeline__) is not tuple:
            cls.__pipeline__ = (cls.__pipeline__,)

        # generate docstrings from all plugin classes
        cls.__doc__ = merge_doc(cls, *reversed(cls.__pipeline__))

        # Follow ``default``, ``extend`` and ``plumb`` declarations.
        pipelines = {}
        defaulted = {}
        for plugin in cls.__pipeline__:
            for name, decor in plugin.__dict__.items():
                if isinstance(decor, extensiondecor):
                    pipe = pipelines.setdefault(name, [])
                    if not pipe or pipe[-1] is not CLOSED:
                        pipe.append(CLOSED)
                    # extend and default close pipelines, i.e no plumbing
                    # methods behind it anymore
                    if isinstance(decor, extend):
                        # collide with an attr that is on the class already,
                        # except if provided by default
                        if name in cls.__dict__ \
                          and name not in defaulted:
                            # XXX: provide more info what is colliding
                            raise PlumbingCollision(name)
                        # put the original attribute on the class
                        setattr(cls, name, decor.attr)
                        # remove potential defaulted flag
                        defaulted.pop(name, None)
                    elif isinstance(decor, default):
                        # set default attribute if there is none yet
                        if not name in cls.__dict__:
                            setattr(cls, name, decor.attr)
                            defaulted[name] = None
                elif name in pipelines and pipelines[name][-1] is CLOSED:
                    raise PlumbingCollision(name)
                elif isinstance(decor, plumbmethod):
                    pipe = pipelines.setdefault(name, [])
                    if pipe and isinstance(pipe[-1], plumbproperty):
                        raise PlumbingCollision(name)
                    # plumbing methods are class methods bound to the plumbing
                    # plugin class, ``getattr`` on the class in combination
                    # with being a classmethod, does this for us.
                    pipe.append(getattr(plugin, name))
                elif isinstance(decor, plumbproperty):
                    pipe = pipelines.setdefault(name, [])
                    if pipe and not isinstance(pipe[-1], plumbproperty):
                        raise PlumbingCollision(name)
                    pipe.append(decor)

            # If zope.interface is available (see import at the beginning of
            # file), we check the plugins for implemented interfaces and make
            # the new class implement these, too.
            if ZOPE_INTERFACE_AVAILABLE:
                ifaces = implementedBy(plugin)
                if ifaces is not None:
                    classImplements(cls, *list(ifaces))

        for name, pipe in pipelines.items():
            # Remove CLOSED pipe marker.
            if pipe[-1] is CLOSED:
                del pipe[-1]

            # Retrieve end point from class, from what happened above it is
            # found with priorities:
            # 1. a plumbing plugin declared it with ``extend``
            # 2. the plumbing class itself declared it
            # 3. a plumbing plugin provided a ``default`` value
            # 4. a base class provides the attribute
            end_point = getattr(cls, name)
            pipe.append(end_point)

            # Finally ``entrance`` will plumb the methods together and return
            # an entrance function, that is set on the plumbing class be and
            # will result in a normal bound method when being retrieved by
            # getattr().
            setattr(cls, name, entrance(name, pipe))
