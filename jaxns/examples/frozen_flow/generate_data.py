from jaxns.gaussian_process.kernels import RBF
from jaxns.gaussian_process.tomographic_kernel.tomographic_kernel import TomographicKernel
import jax.numpy as jnp
from jax import random

from jaxns.gaussian_process.utils import make_coord_array
from jaxns.utils import msqrt


def rbf_dtec(nant, ndir, ntime, height, width, sigma, l, uncert, v):
    """
    In frozen flow the screen moves with velocity v.
    fed(x,t) = fed(x-v*t,0)
    so that the  tomographic kernel transforms as,
    K(x1,k1,t1,x2,k2,t2) = K(x1-v * t1,k1,0,x2-v * t2,k2,0)
    """
    import pylab as plt
    a = jnp.concatenate([10.*random.uniform(random.PRNGKey(0), shape=(nant,2)),jnp.zeros((nant, 1))], axis=1)
    k = jnp.concatenate([4.*jnp.pi/180.*random.uniform(random.PRNGKey(0), shape=(ndir,2), minval=-1, maxval=1),jnp.ones((ndir, 1))], axis=1)
    k = k / jnp.linalg.norm(k,axis=1, keepdims=True)
    t = jnp.arange(ntime)[:,None]*30.#seconds
    X = make_coord_array(a, k, t)
    a = X[:,0:3]
    k = X[:,3:6]
    t = X[:,6:7]
    x0 = a[0,:]
    kernel = TomographicKernel(x0, RBF(), S_marg=100, S_gamma=100)
    K = kernel(X[:,:6]-jnp.concatenate([v, jnp.zeros(3)])*t, X[:,:6]-jnp.concatenate([v, jnp.zeros(3)])*t, height, width, l, sigma)
    plt.imshow(K)
    plt.colorbar()
    plt.show()
    plt.plot(jnp.sqrt(jnp.diag(K)))
    plt.show()


    L = msqrt(K)#jnp.linalg.cholesky(K + jnp.eye(K.shape_dict[0])*1e-3)

    tec = L @ random.normal(random.PRNGKey(2), shape=(L.shape[0],))
    tec = tec.reshape((nant, ndir, ntime))
    dtec = tec - tec[0,:, :]
    dtec = dtec.reshape((-1,))
    plt.plot(dtec)
    plt.show()
    return X, dtec, dtec + uncert*random.normal(random.PRNGKey(3),shape=dtec.shape)

def main():
    rbf_dtec(10,10,200., 100., 0.3, 10., 1.)

if __name__=='__main__':
    main()
