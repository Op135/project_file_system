from functools import wraps

def decorator_name(func):
    # decorator_name 装饰器名称，可随意更改
    # func 就是雇佣装饰器在其上面的函数体
    
    # 给 wrapper 函数施了一个魔法，让它把自己的身份信息（比如它的名字 __name__、它的文档字符串 __doc__）
    # 伪装成原始函数 func 的样子，从而“物归原主”；不是运行必须的，但却是编写健壮、可维护装饰器的标准做法
    @wraps(func)
    # *args：会把所有位置参数（比如 my_func(10, "hello")）打包成一个元组 (10, "hello")
    # **kwargs：会把所有关键字参数（比如 my_func(user="Tom", score=95)）打包成一个字典 {'user': 'Tom', 'score': 95}
    # wrapper 是一种代码规范和惯例，能让别人一眼就看懂你的代码结构
    def wrapper(*args, **kwargs): # 接收任意参数
        
        # 可在执行原始函数前先执行某些代码，写在这里就行

        # 执行原始函数本身
        # 这是关键！我们调用原始函数，并把它的运行结果（返回值）用一个变量存起来。
        result = func(*args, **kwargs)

        # 可在实行原始函数后再执行某些代码，写在这里就行
        
        # 把原始函数的运行结果返回出去
        # 如果没有这一步，那么调用被装饰的函数将得不到任何返回值。
        return result

    # 必须返回上面定义的wrapper函数，名字要相同
    return wrapper

# 雇佣一个装饰器函数
@decorator_name
def fun_name():
    # 函数执行代码
    return "返回值"
