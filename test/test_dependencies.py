from nameko.dependencies import (
    dependency_decorator, get_decorator_providers, DecoratorDependency)


class FooProvider(DecoratorDependency):
    def __init__(self, args, kwargs):
        self.args = args
        self.kwargs = kwargs


class CheeseProvider(DecoratorDependency):
    pass


@dependency_decorator
def foobar(*args, **kwargs):
    """foobar-doc"""
    return FooProvider(args, kwargs)


@dependency_decorator
def cheese():
    return CheeseProvider()


def test_dependency_decorator():
    # make sure foobar is properly wrapped
    assert foobar.__doc__ == 'foobar-doc'
    assert foobar.func_name == 'foobar'

    def foo(spam):
        pass

    decorated_foo = foobar(foo='bar')(foo)

    # make sure dependency_deocorator passes through the decorated method
    assert decorated_foo is foo


def test_get_decorator_providers():

    class Foobar(object):
        @foobar('spam', oof="rab")
        def shrub(self, arg):
            return arg

        @cheese
        def ni(self, arg):
            return arg

    foo = Foobar()

    providers = list(get_decorator_providers(foo))
    assert len(providers) == 2

    name, provider = providers.pop()
    assert name == "shrub"
    assert isinstance(provider, FooProvider)
    assert provider.args == ("spam",)
    assert provider.kwargs == {"oof": "rab"}
    assert foo.shrub(1) == 1

    name, provider = providers.pop()
    assert name == "ni"
    assert isinstance(provider, CheeseProvider)
    assert foo.ni(2) == 2