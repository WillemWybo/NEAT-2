"""
File contains:

    - :class:`CompartmentNode`
    - :class:`CompartmentTree`

Author: W. Wybo
"""


import numpy as np
import scipy.linalg as la
import scipy.optimize as so
import sympy as sp

from .stree import SNode, STree
from neat.channels import channelcollection
from neat.tools import kernelextraction as ke

import copy
import warnings
import itertools
from operator import mul
from functools import reduce


class RowMat(object):
    def __init__(self, row, ii):
        self.row = row
        self.ii = ii

    def __mul__(self, rowmat):
        row_res = self.row[rowmat.ii] * rowmat.row
        return RowMat(row_res, self.ii)

    def trace(self):
        return self.row[self.ii]


class CompartmentNode(SNode):
    '''
    Implements a node for :class:`CompartmentTree`

    Attributes
    ----------
        ca: float
            capacitance of the compartment (uF)
        g_l: float
            leak conductance at the compartment (uS)
        g_c: float
            Coupling conductance of compartment with parent compartment (uS).
            Ignore if node is the root
    '''
    def __init__(self, index, loc_ind=None, ca=1., g_c=0., g_l=1e-2, e_eq=-75.):
        super(CompartmentNode, self).__init__(index)
        # location index this node corresponds to
        self.loc_ind = loc_ind
        # compartment params
        self.ca = ca   # capacitance (uF)
        self.g_c = g_c # coupling conductance (uS)
        # self.g_l = g_l # leak conductance (uS)
        self.e_eq = e_eq # equilibrium potential (mV)
        self.currents = {'L': [g_l, e_eq]} # ion channel currents and reversals
        self.concmechs = {}
        self.expansion_points = {}

    def __str__(self, with_parent=False, with_children=False):
        node_string = super(CompartmentNode, self).__str__()
        if self.parent_node is not None:
            node_string += ', Parent: ' + super(CompartmentNode, self.parent_node).__str__()
        node_string += ' --- (g_c = %.12f uS, '%self.g_c + \
                       ', '.join(['g_' + cname + ' = %.12f uS'%cpar[0] \
                            for cname, cpar in self.currents.items()]) + \
                       ', c = %.12f uF)'%self.ca
        return node_string

    def addCurrent(self, channel_name, e_rev=None, channel_storage=None):
        if channel_name is not 'L':
            if e_rev is None:
                e_rev = channelcollection.E_REV_DICT[channel_name]
            self.currents[channel_name] = [0., e_rev]
            if channel_storage is not None and channel_name not in channel_storage:
                channel_storage[channel_name] = \
                                eval('channelcollection.' + channel_name + '()')
            self.expansion_points[channel_name] = None

    def addConcMech(self, ion, params={}):
        '''
        Add a concentration mechanism at this node.

        Parameters
        ----------
        ion: string
            the ion the mechanism is for
        params: dict
            parameters for the concentration mechanism (only used for NEURON model)
        '''
        if set(params.keys()) == {'gamma', 'tau'}:
            self.concmechs[ion] = concmechs.ExpConcMech(ion,
                                        params['tau'], params['gamma'])
        else:
            warnings.warn('These parameters do not match any NEAT concentration ' + \
                          'mechanism, no concentration mechanism has been added', UserWarning)

    def getCurrent(self, channel_name, channel_storage=None):
        '''
        Returns an ``::class::neat.channels.ionchannels.IonChannel`` object. If
        `channel_storage` is given,

        Parameters
        ----------
        channel_name: string
            the name of the ion channel
        channel_storage: dict of ionchannels (optional)
            keys are the names of the ion channels, and values the channel
            instances
        '''
        try:
            return channel_storage[channel_name]
        except (KeyError, TypeError):
            return eval('channelcollection.' + channel_name + '()')

    def setExpansionPoint(self, channel_name, statevar='asymptotic', channel_storage=None):
        '''
        Set the choice for the state variables of the ion channel around which
        to linearize.

        Note that when adding an ion channel to the node, the
        default expansion point setting is to linearize around the asymptotic values
        for the state variables at the equilibrium potential store in `self.e_eq`.
        Hence, this function only needs to be called to change that setting.

        Parameters
        ----------
        channel_name: string
            the name of the ion channel
        statevar: `np.ndarray`, `'max'` or `'asymptotic'` (default)
            If `np.ndarray`, should be of the same shape as the ion channels'
            state variables array, if `'max'`, the point at which the
            linearized channel current is maximal for the given equilibirum potential
            `self.e_eq` is used. If `'asymptotic'`, linearized around the asymptotic values
            for the state variables at the equilibrium potential
        channel_storage: dict of ion channels (optional)
            The ion channels that have been initialized already. If not
            provided, a new channel is initialized

        Raises
        ------
        KeyError: if `channel_name` is not in `self.currents`
        '''
        if isinstance(statevar, str):
            if statevar == 'asymptotic':
                statevar = None
            elif statevar == 'max':
                channel = self.getCurrent(channel_name, channel_storage=channel_storage)
                statevar = channel.findMaxCurrentVGiven(self.e_eq, self.freqs,
                                                        self.currents[channel_name][1])
        self.expansion_points[channel_name] = statevar

    def calcMembraneConductanceTerms(self, freqs=0.,
                                     channel_names=None, channel_storage=None):
        '''
        Compute the membrane impedance terms and return them as a `dict`

        Parameters
        ----------
        freqs: np.ndarray (ndim = 1, dtype = complex or float) or float or complex
            The frequencies at which the impedance terms are to be evaluated
        channel_storage: dict of ion channels (optional)
            The ion channels that have been initialized already. If not
            provided, a new channel is initialized

        Returns
        -------
        dict of np.ndarray or float or complex
            Each entry in the dict is of the same type as ``freqs`` and is the
            conductance term of a channel
        '''
        if channel_names is None: channel_names = list(self.currents.keys())
        cond_terms = {}
        if 'L' in channel_names:
            cond_terms['L'] = 1. # leak conductance has 1 as prefactor
        for channel_name in set(channel_names) - set('L'):
            if channel_name not in self.currents:
                self.addCurrent(channel_name, channel_storage=channel_storage)
            e = self.currents[channel_name][1]
            # create the ionchannel object
            channel = self.getCurrent(channel_name, channel_storage=channel_storage)
            # check if needs to be computed around expansion point
            sv = self.expansion_points[channel_name]
            # add channel contribution to membrane impedance
            cond_terms[channel_name] = - channel.computeLinSum(self.e_eq, freqs, e,
                                                               statevars=sv)

        return cond_terms

    def calcMembraneConcentrationTerms(self, ion, channel_names=None, freqs=0.,
                                        channel_storage=None):
        if channel_names is None: channel_names = list(self.currents.keys())
        conc_write_channels = np.zeros_like(freqs)
        conc_read_channels  = np.zeros_like(freqs)
        for channel_name, (g, e) in self.currents.items():
            if channel_name in channel_names and channel_name != 'L':
                channel = self.getCurrent(channel_name, channel_storage=channel_storage)
                # check if needs to be computed around expansion point
                sv = self.expansion_points[channel_name]
                # if the channel adds to ion channel current, add it here
                if channel.ion == ion:
                    conc_write_channels += g * channel.computeLinSum(self.e_eq, freqs, e,
                                                                     statevars=sv)
                    # conc_write_channels += g * channel.computePOpen(self.e_eq, statevars=sv)
                # if channel reads the ion channel current, add it here
                if ion in channel.concentrations:
                    conc_read_channels -= g * channel.computeLinConc(self.e_eq, freqs, e, ion,
                                                                     statevars=sv)

        return conc_write_channels * \
               conc_read_channels * \
               self.concmechs[ion].computeLin(freqs)

    def getGTot(self, v=None, channel_names=None, channel_storage=None):
        if channel_names is None: channel_names = list(self.currents.keys())
        g_tot = self.currents['L'][0] if 'L' in channel_names else 0.
        v = self.e_eq if v is None else v
        for channel_name in channel_names:
            if channel_name != 'L':
                g, e = self.currents[channel_name]
                # create the ionchannel object
                channel = self.getCurrent(channel_name, channel_storage=channel_storage)
                # check if needs to be computed around expansion point
                sv = self.expansion_points[channel_name]
                g_tot += g * channel.computePOpen(v, statevars=sv)

        return g_tot

    def setGTot(self, illegal):
        raise AttributeError("`g_tot` is a read-only attribute, set the leak " + \
                             "conductance by calling ``func:addCurrent`` with " + \
                             " \'L\' as `current_type`")

    g_tot = property(getGTot, setGTot)

    def getITot(self, v=None, channel_names=None, channel_storage=None, p_open_channels={}):
        if channel_names is None: channel_names = list(self.currents.keys())
        v = self.e_eq if v is None else v
        i_tot = self.currents['L'][0] * (v - self.currents['L'][1]) if 'L' in channel_names else 0.
        for channel_name in channel_names:
            if channel_name != 'L':
                g, e = self.currents[channel_name]
                if channel_name not in p_open_channels:
                    # create the ionchannel object
                    channel = self.getCurrent(channel_name, channel_storage=channel_storage)
                    # check if needs to be computed around expansion point
                    sv = self.expansion_points[channel_name]
                    i_tot += g * channel.computePOpen(v, statevars=sv) * (v - e)
                else:
                    i_tot += g * p_open_channels[channel_name] * (v - e)

        return i_tot

    def getDrive(self, channel_name, v=None, channel_storage=None):
        v = self.e_eq if v is None else v
        _, e = self.currents[channel_name]
        # create the ionchannel object
        channel = self.getCurrent(channel_name, channel_storage=channel_storage)
        sv = self.expansion_points[channel_name]
        return channel.computePOpen(v, statevars=sv) * (v - e)

    # def getITot(self, v=None, channel_names=None, channel_storage=None):
    #     if channel_names is None: channel_names = self.currents.keys()
    #     v = self.e_eq if v is None else v
    #     i_tot = self.currents['L'][0] * (v - self.currents['L'][1]) \
    #             if 'L' in channel_names else 0.
    #     for channel_name in channel_names:
    #         if channel_name != 'L':
    #             g, e = self.currents[channel_name]
    #             # create the ionchannel object
    #             if channel_storage is not None:
    #                 channel = channel_storage[channel_name]
    #             else:
    #                 channel = eval('channelcollection.' + channel_name + '()')
    #             i_tot += g * channel.computePOpen(v) * (v - e)

    #     return i_tot

    def getDynamicDrive(self, channel_name, p_open, v):
        assert p_open.shape == v.shape
        _, e = self.currents[channel_name]
        return p_open * (v - e)


    def getDynamicDrive_(self, channel_name, v, dt, channel_storage=None):
        # assert p_open.shape == v.shape
        _, e = self.currents[channel_name]
        if channel_storage is not None:
            channel = channel_storage[channel_name]
        else:
            channel = eval('channelcollection.' + channel_name + '()')
        # sv = np.zeros(list(channel.statevars.shape) + list(v.shape))
        # p_open = np.zeros_like(v)
        # for tt in range(len(v.shape)):
        #     if tt == 0:
        #         sv_inf_prev = channel.computeVarInf(v[:,tt])
        #         tau_prev= channel.computeTauInf(v[:,tt])
        #     else:
        #         sv_inf = channel.computeVarInf(v[:,tt])
        #         tau = channel.computeTauInf(v[:,tt])
        #         sv_inf_aux = (sv_inf + sv_inf_prev) / 2.
        #         tau_aux = (tau + tau_prev) / 2.
        #         sv[:,:,tt] = (sv[:,tt-1] + dt * sv_inf_aux / tau_aux) / (1. + dt / tau_aux)
        #         p_open[:,tt] = channel.computePOpen(v[:,tt], statevars=sv[:,:,tt])
        #         sv_inf_prev = sv_inf
        #         tau_prev = tau
        # storage
        p_open = np.zeros_like(v)
        # initialize
        sv_inf_prev = channel.computeVarInf(v[0])
        tau_prev = channel.computeTauInf(v[0])
        sv = sv_inf_prev
        p_open[0] = channel.computePOpen(v[0], statevars=sv)
        for tt in range(1,len(v)):
            sv_inf = channel.computeVarInf(v[tt])
            tau = channel.computeTauInf(v[tt])
            # sv_inf_aux = (sv_inf + sv_inf_prev) / 2.
            f_aux  = -2. / (tau + tau_prev)
            h_prev = sv_inf_prev / tau_prev
            h_now  = sv_inf / tau
            # sv[:,:,tt] = (sv[:,:,tt-1] + dt * sv_inf_aux / tau_aux) / (1. + dt / tau_aux)
            p0_aux = np.exp(f_aux * dt)
            p1_aux = (1. - p0_aux) / (f_aux**2 * dt)
            p2_aux = p0_aux / f_aux + p1_aux
            p3_aux = -1. / f_aux - p1_aux
            # next step sv
            sv = p0_aux * sv + p2_aux * h_prev + p3_aux * h_now
            # store for next step
            sv_inf_prev = sv_inf
            tau_prev = tau
            # store open probability
            p_open[tt] = channel.computePOpen(v[tt], statevars=sv)

        return p_open * (v - e)

    def getDynamicI(self, channel_name, p_open, v):
        assert p_open.shape == v.shape
        g, e = self.currents[channel_name]
        return g * p_open * (v - e)

    def fitEL(self, channel_storage=None):
        i_eq = 0.
        for channel_name in set(self.currents.keys()) - set('L'):
            g, e = self.currents[channel_name]
            # create the ionchannel object
            if channel_storage is not None:
                channel = channel_storage[channel_name]
            else:
                channel = eval('channelcollection.' + channel_name + '()')
            # compute channel conductance and current
            p_open = channel.computePOpen(self.e_eq)
            i_chan = g * p_open * (e - self.e_eq)
            i_eq += i_chan
        e_l = self.e_eq - i_eq / self.currents['L'][0]
        self.currents['L'][1] = e_l


class CompartmentTree(STree):
    def __init__(self, root=None):
        super(CompartmentTree, self).__init__(root=root)
        self.channel_storage = {}
        # for fitting the model
        self.resetFitData()

    def createCorrespondingNode(self, index, ca=1., g_c=0., g_l=1e-2):
        '''
        Creates a node with the given index corresponding to the tree class.

        Parameters
        ----------
            node_index: int
                index of the new node
        '''
        return CompartmentNode(index, ca=ca, g_c=g_c, g_l=g_l)

    def setEEq(self, e_eq):
        if isinstance(e_eq, float) or isinstance(e_eq, int):
            e_eq = e_eq * np.ones(len(self), dtype=float)
        else:
            e_eq = self._permuteToTree(np.array(e_eq))
        for ii, node in enumerate(self): node.e_eq = e_eq[ii]

    def getEEq(self):
        return np.array([node.e_eq for node in self])

    def setExpansionPoints(self, expansion_points):
        to_tree_inds = self._permuteToTreeInds()
        for channel_name, expansion_point in expansion_points.items():
            # if one set of state variables, set throughout neuron
            if isinstance(expansion_point, str) or \
               expansion_point is None:
                svs = np.array([expansion_point for _ in self])
            elif isinstance(expansion_point, np.ndarray):
                if expansion_point.ndim == 3:
                    svs = np.array(expansion_point)
                elif expansion_point.ndim == 2:
                    svs = np.array([expansion_point for _ in self])
            for node, sv in zip(self, svs[to_tree_inds]):
                node.setExpansionPoint(channel_name, statevar=sv,
                                       channel_storage=self.channel_storage)

    def removeExpansionPoints(self):
        for node in self:
            for channel_name in node.currents:
                node.setExpansionPoint(channel_name, statevar='asymptotic')

    def fitEL(self, p_open_channels={}):
        '''
        Set the leak reversal potential to obtain the desired equilibrium
        potentials
        '''
        for chan, p_o in p_open_channels.items():
            p_open_channels[chan] = self._permuteToTree(p_o)
        e_l_0 = self.getEEq()
        # compute the solutions
        fun = self._fun(e_l_0, p_open_channels=p_open_channels)
        jac = self._jac(e_l_0)
        e_l = np.linalg.solve(jac, -fun + np.dot(jac, e_l_0))
        # set the leak reversals
        for ii, node in enumerate(self):
            node.currents['L'][1] = e_l[ii]

    def _fun(self, e_l, p_open_channels={}):
        # set the leak reversal potentials
        for ii, node in enumerate(self):
            node.currents['L'][1] = e_l[ii]
        # compute the function values (currents)
        fun_vals = np.zeros(len(self))
        for ii, node in enumerate(self):
            p_o_c = {chan: p_o[ii] for chan, p_o in p_open_channels.items()}
            fun_vals[ii] += node.getITot(p_open_channels=p_o_c)
            # add the parent node coupling term
            if node.parent_node is not None:
                fun_vals[ii] += node.g_c * (node.e_eq - node.parent_node.e_eq)
            # add the child node coupling terms
            for cnode in node.child_nodes:
                fun_vals[ii] += cnode.g_c * (node.e_eq - cnode.e_eq)
        return fun_vals

    def _jac(self, e_l):
        for ii, node in enumerate(self):
            node.currents['L'][1] = e_l[ii]
        jac_vals = np.array([-node.currents['L'][0] for node in self])
        return np.diag(jac_vals)

    def addCurrent(self, channel_name, e_rev=None):
        '''
        Add an ion channel current to the tree

        Parameters
        ----------
        channel_name: string
            The name of the channel type
        '''
        for ii, node in enumerate(self):
            node.addCurrent(channel_name, e_rev=e_rev,
                            channel_storage=self.channel_storage)

    def addConcMech(self, ion, params={}):
        '''
        Add a concentration mechanism to the tree

        Parameters
        ----------
        ion: string
            the ion the mechanism is for
        params: dict
            parameters for the concentration mechanism (only used for NEURON model)
        '''
        for node in self: node.addConcMech(ion, params=params)

    def _permuteToTreeInds(self):
        return np.array([node.loc_ind for node in self])

    def _permuteToTree(self, mat):
        '''
        give index list that can be used to permutate the axes of the impedance
        and system matrix to correspond to the associated set of locations
        '''
        index_arr = self._permuteToTreeInds()
        if mat.ndim == 1:
            return mat[index_arr]
        elif mat.ndim == 2:
            return mat[index_arr,:][:,index_arr]
        elif mat.ndim == 3:
            return mat[:,index_arr,:][:,:,index_arr]

    def _permuteToLocsInds(self):
        loc_inds = np.array([node.loc_ind for node in self])
        return np.argsort(loc_inds)

    def _permuteToLocs(self, mat):
        index_arr = self._permuteToLocsInds()
        if mat.ndim == 1:
            return mat[index_arr]
        elif mat.ndim == 2:
            return mat[index_arr,:][:,index_arr]
        elif mat.ndim == 3:
            return mat[:,index_arr,:][:,:,index_arr]

    def getEquivalentLocs(self):
        loc_inds = [node.loc_ind for node in self]
        index_arr = np.argsort(loc_inds)
        locs_unordered = [(node.index, .5) for node in self]
        return [locs_unordered[ind] for ind in index_arr]

    def calcImpedanceMatrix(self, freqs=0., channel_names=None, indexing='locs',
                                use_conc=False):
        return np.linalg.inv(self.calcSystemMatrix(freqs=freqs,
                             channel_names=channel_names, indexing=indexing,
                             use_conc=use_conc))

    def calcConductanceMatrix(self, indexing='locs'):
        '''
        Constructs the conductance matrix of the model

        Returns
        -------
            np.ndarray (dtype = float, ndim = 2)
                the conductance matrix
        '''
        g_mat = np.zeros((len(self), len(self)))
        for node in self:
            ii = node.index
            g_mat[ii, ii] += node.g_tot + node.g_c
            if node.parent_node is not None:
                jj = node.parent_node.index
                g_mat[jj,jj] += node.g_c
                g_mat[ii,jj] -= node.g_c
                g_mat[jj,ii] -= node.g_c
        if indexing == 'locs':
            return self._permuteToLocs(g_mat)
        elif indexing == 'tree':
            return g_mat
        else:
            raise ValueError('invalid argument for `indexing`, ' + \
                             'has to be \'tree\' or \'locs\'')

    def calcSystemMatrix(self, freqs=0., channel_names=None,
                               with_ca=True, use_conc=False,
                               indexing='locs', add_L=True):
        '''
        Constructs the matrix of conductance and capacitance terms of the model
        for each frequency provided in ``freqs``. this matrix is evaluated at
        the equilibrium potentials stored in each node

        Parameters
        ----------
            freqs: np.array (dtype = complex) or float
                Frequencies at which the matrix is evaluated [Hz]
            channel_names: `None` or `list` of `str`
                The channels to be included in the matrix
            with_ca: `bool`
                Whether or not to include the capacitive currents
            use_conc: `bool`
                wheter or not to use the concentration dynamics
            indexing: 'tree' or 'locs'
                Whether the indexing order of the matrix corresponds to the tree
                nodes (order in which they occur in the iteration) or to the
                locations on which the reduced model is based
            add_L: bool (default `True`)
                whether to always add the leak conductance to the matrix
                calculation, even when it is not in channel_names

        Returns
        -------
            np.ndarray (ndim = 3, dtype = complex)
                The first dimension corresponds to the
                frequency, the second and third dimension contain the impedance
                matrix for that frequency
        '''
        no_freq_dim = False
        if isinstance(freqs, float) or isinstance(freqs, complex):
            freqs = np.array([freqs])
            no_freq_dim = True
        if channel_names is None:
            channel_names = ['L'] + list(self.channel_storage.keys())
        if add_L:
            channel_names = ['L'] + copy.deepcopy(channel_names)
        s_mat = np.zeros((len(freqs), len(self), len(self)), dtype=freqs.dtype)
        for node in self:
            ii = node.index
            # set the capacitance contribution
            if with_ca: s_mat[:,ii,ii] += freqs * node.ca
            # set the coupling conductances
            s_mat[:,ii,ii] += node.g_c
            if node.parent_node is not None:
                jj = node.parent_node.index
                s_mat[:,jj,jj] += node.g_c
                s_mat[:,ii,jj] -= node.g_c
                s_mat[:,jj,ii] -= node.g_c
            # set the ion channel contributions
            g_terms = node.calcMembraneConductanceTerms(freqs=freqs,
                                    channel_names=channel_names,
                                    channel_storage=self.channel_storage)
            s_mat[:,ii,ii] += sum([node.currents[c_name][0] * g_term \
                                   for c_name, g_term in g_terms.items()])
            if use_conc:
                for ion, concmech in node.concmechs.items():
                    c_term = node.calcMembraneConcentrationTerms(ion, freqs=freqs,
                                        channel_names=channel_names,
                                        channel_storage=self.channel_storage)
                    s_mat[:,ii,ii] += concmech.gamma * c_term
        if indexing == 'locs':
            return self._permuteToLocs(s_mat[0,:,:]) if no_freq_dim else \
                   self._permuteToLocs(s_mat)
        elif indexing == 'tree':
            return s_mat[0,:,:] if no_freq_dim else s_mat
        else:
            raise ValueError('invalid argument for `indexing`, ' + \
                             'has to be \'tree\' or \'locs\'')

    def calcEigenvalues(self, indexing='tree'):
        '''
        Calculates the eigenvalues and eigenvectors of the passive system

        Returns
        -------
        np.ndarray (ndim = 1, dtype = complex)
            the eigenvalues
        np.ndarray (ndim = 2, dtype = complex)
            the right eigenvector matrix
        indexing: 'tree' or 'locs'
            Whether the indexing order of the matrix corresponds to the tree
            nodes (order in which they occur in the iteration) or to the
            locations on which the reduced model is based
        '''
        # get the system matrix
        mat = self.calcSystemMatrix(freqs=0., channel_names=['L'],
                                    with_ca=False, indexing=indexing)
        ca_vec = np.array([node.ca for node in self])
        if indexing == 'locs':
            ca_vec = self._permuteToLocs(ca_vec)
        mat /= ca_vec[:,None]
        # mat = mat.astype(complex)
        # compute the eigenvalues
        alphas, phimat = la.eig(mat)
        if max(np.max(np.abs(alphas.imag)), np.max(np.abs(phimat.imag))) < 1e-5:
            alphas = alphas.real
            phimat = phimat.real
        phimat_inv = la.inv(phimat)
        # print '\n>>> eig'
        # print alphas
        # print '>>> phimat'
        # print phimat

        # print '\n>>> eig original mat'
        # print mat
        # print '>>> eig reconstructed mat'
        # print np.dot(phimat, np.dot(np.diag(alphas), np.linalg.inv(phimat)))
        # print '>>> test orhogonal'
        # print np.dot(phimat, np.linalg.inv(phimat))
        # print ''

        alphas /= -1e3
        phimat_inv /= ca_vec[None,:] * 1e3
        return alphas, phimat, phimat_inv

    def _calcConvolution(self, dt, inputs):
        '''
        Compute the convolution of the `inputs` with the impedance matrix of the
        passive system

        Parameters
        ----------
        inputs: np.ndarray (ndim = 3)
            The inputs. First dimension is the input site (tree indices) and
            last dimension is time. Middle dimension can be arbitrary. Convolution
            is computed for all elements on the first 2 axes

        Return
        ------
        np.ndarray (ndim = 4)
            The convolutions
        '''
        # compute the system eigenvalues for convolution
        alphas, phimat, phimat_inv = self.calcEigenvalues()
        # print '\n>>> z_mat eig'
        # print np.dot(phimat, np.dot(np.diag(-1./alphas), phimat_inv))
        # print '>>> z_mat direct'
        # print self.calcImpedanceMatrix(freqs=0., indexing='tree', channel_names=['L'])
        # print '>>> z_mat ratio'
        # print np.dot(phimat, np.dot(np.diag(-1./alphas), phimat_inv)) / self.calcImpedanceMatrix(freqs=0., indexing='tree', channel_names=['L'])
        # print '\n>>> g_mat eig'
        # print np.dot(phimat, np.dot(np.diag(-alphas), phimat_inv))
        # print '>>> g_mat direct'
        # mat = self.calcSystemMatrix(freqs=0., channel_names=['L'],
        #                             with_ca=False, indexing='tree')
        # mat /= np.array([node.ca for node in self])[:,None]
        # print mat
        # propagator s to compute convolution
        p0 = np.exp(alphas*dt)
        # p1_ = - (1. - p0) / alphas
        p1 = - 1. / alphas + (p0 - 1.) / (alphas**2 * dt)
        p2 =   p0 / alphas - (p0 - 1.) / (alphas**2 * dt)
        p_ = - 1. / alphas
        # multiply input matrix with phimat (original indices kct)
        inputs = np.einsum('nk,kct->nkct', phimat_inv, inputs)
        inputs = np.einsum('ln,nkct->lnkct', phimat, inputs)
        inputs = np.moveaxis(inputs, -1, 0) #tlnkc
        # do the convolution
        convres = np.zeros_like(inputs)
        # convvar = np.zeros(inputs.shape[1:])
        convvar = np.einsum('n,lnkc->lnkc', p_, inputs[0])
        convres[0] = convvar
        for kk, inp in enumerate(inputs[1:]):
            inp_prev = inputs[kk]
            convvar = np.einsum('n,lnkc->lnkc', p0, convvar) + \
                      np.einsum('n,lnkc->lnkc', p1, inp) + \
                      np.einsum('n,lnkc->lnkc', p2, inp_prev)
            convres[kk+1] = convvar
        # recast result
        # convres = np.sum(convres, axis=2)
        convres = np.einsum('tlnkc->tlkc', convres)
        convres = np.moveaxis(convres, 0, -1) # lkct

        return convres.real

    def _preprocessZMatArg(self, z_mat_arg):
        if isinstance(z_mat_arg, np.ndarray):
            return [self._permuteToTree(z_mat_arg)]
        elif isinstance(z_mat_arg, list):
            return [self._permuteToTree(z_mat) for z_mat in z_mat_arg]
        else:
            raise ValueError('`z_mat_arg` has to be ``np.ndarray`` or list of ' + \
                             '`np.ndarray`')

    def _preprocessEEqs(self, e_eqs, w_e_eqs=None):
        # preprocess e_eqs argument
        if e_eqs is None:
            e_eqs = np.array([self.getEEq()])
        if isinstance(e_eqs, float):
            e_eqs = np.array([e_eqs])
        elif isinstance(e_eqs, list) or isinstance(e_eqs, tuple):
            e_eqs = np.array(e_eqs)
        elif isinstance(e_eqs, np.ndarray):
            pass
        else:
            raise TypeError('`e_eqs` has to be ``float`` or list or ' + \
                             '``np.ndarray`` of ``floats`` or ``np.ndarray``')
        # preprocess the w_e_eqs argument
        if w_e_eqs is None:
            w_e_eqs = np.ones_like(e_eqs)
        elif isinstance(w_e_eqs, float):
            w_e_eqs = np.array([e_eqs])
        elif isinstance(w_e_eqs, list) or isinstance(w_e_eqs, tuple):
            w_e_eqs = np.array(w_e_eqs)
        # check if arrays have the same shape
        assert w_e_eqs.shape[0] == e_eqs.shape[0]

        return e_eqs, w_e_eqs

    def _preprocessFreqs(self, freqs, w_freqs=None, z_mat_arg=None):
        if isinstance(freqs, float) or isinstance(freqs, complex):
            freqs = np.array([freqs])
        if w_freqs is None:
            w_freqs = np.ones_like(freqs)
        else:
            assert w_freqs.shape[0] == freqs.shape[0]
        # convert to 3d matrices if they are two dimensional
        z_mat_arg_ = []
        for z_mat in z_mat_arg:
            if z_mat.ndim == 2:
                z_mat_arg_.append(z_mat[np.newaxis,:,:])
            else:
                z_mat_arg_.append(z_mat)
            assert z_mat_arg_[-1].shape[0] == freqs.shape[0]
        z_mat_arg = z_mat_arg_
        return freqs, w_freqs, z_mat_arg


    def computeGMC(self, z_mat_arg, e_eqs=None, channel_names=['L']):
        '''
        Fit the models' membrane and coupling conductances to a given steady
        state impedance matrix.

        Parameters
        ----------
        z_mat_arg: np.ndarray (ndim = 2, dtype = float or complex) or
                   list of np.ndarray (ndim = 2, dtype = float or complex)
            If a single array, represents the steady state impedance matrix,
            If a list of arrays, represents the steady state impedance
            matrices for each equilibrium potential in ``e_eqs``
        e_eqs: np.ndarray (ndim = 1, dtype = float) or float
            The equilibirum potentials in each compartment for each
            evaluation of ``z_mat``
        channel_names: list of string (defaults to ['L'])
            Names of the ion channels that have been included in the impedance
            matrix calculation and for whom the conductances are fit. Default is
            only leak conductance
        '''
        z_mat_arg = self._preprocessZMatArg(z_mat_arg)
        e_eqs, _ = self._preprocessEEqs(e_eqs)
        assert len(z_mat_arg) == len(e_eqs)
        # do the fit
        mats_feature = []
        vecs_target = []
        for z_mat, e_eq in zip(z_mat_arg, e_eqs):
            # set equilibrium conductances
            self.setEEq(e_eq)
            # create the matrices for linear fit
            g_struct = self._toStructureTensorGMC(channel_names)
            tensor_feature = np.einsum('ij,jkl->ikl', z_mat, g_struct)
            tshape = tensor_feature.shape
            mat_feature_aux = np.reshape(tensor_feature,
                                         (tshape[0]*tshape[1], tshape[2]))
            vec_target_aux = np.reshape(np.eye(len(self)), (len(self)*len(self),))
            mats_feature.append(mat_feature_aux)
            vecs_target.append(vec_target_aux)
        mat_feature = np.concatenate(mats_feature, 0)
        vec_target = np.concatenate(vecs_target)
        # linear regression fit
        # res = la.lstsq(mat_feature, vec_target)
        res = so.nnls(mat_feature, vec_target)
        g_vec = res[0].real
        # set the conductances
        self._toTreeGMC(g_vec, channel_names)

    def _toStructureTensorGMC(self, channel_names):
        g_vec = self._toVecGMC(channel_names)
        g_struct = np.zeros((len(self), len(self), len(g_vec)))
        kk = 0 # counter
        for node in self:
            ii = node.index
            g_terms = node.calcMembraneConductanceTerms(0.,
                                        channel_storage=self.channel_storage,
                                        channel_names=['L']+channel_names)
            if node.parent_node == None:
                # membrance conductance elements
                for channel_name in channel_names:
                    g_struct[0, 0, kk] += g_terms[channel_name]
                    kk += 1
            else:
                jj = node.parent_node.index
                # coupling conductance element
                g_struct[ii, jj, kk] -= 1.
                g_struct[jj, ii, kk] -= 1.
                g_struct[jj, jj, kk] += 1.
                g_struct[ii, ii, kk] += 1.
                kk += 1
                # membrance conductance elements
                for channel_name in channel_names:
                    g_struct[ii, ii, kk] += g_terms[channel_name]
                    kk += 1
        return g_struct

    def _toVecGMC(self, channel_names):
        '''
        Place all conductances to be fitted in a single vector
        '''
        g_list = []
        for node in self:
            if node.parent_node is None:
                g_list.extend([node.currents[c_name][0] for c_name in channel_names])
            else:
                g_list.extend([node.g_c] + \
                              [node.currents[c_name][0] for c_name in channel_names])
        return np.array(g_list)

    def _toTreeGMC(self, g_vec, channel_names):
        kk = 0 # counter
        for ii, node in enumerate(self):
            if node.parent_node is None:
                for channel_name in channel_names:
                    node.currents[channel_name][0] = g_vec[kk]
                    kk += 1
            else:
                node.g_c = g_vec[kk]
                kk += 1
                for channel_name in channel_names:
                    node.currents[channel_name][0] = g_vec[kk]
                    kk += 1

    def _preprocessExpansionPoints(self, svs, e_eqs):
        if svs is None:
            svs = [None for _ in e_eqs]
        elif isinstance(svs, list):
            svs = np.array(svs)
            assert svs.shape[0] == e_eqs.shape[0]
        elif isinstance(svs, np.ndarray):
            assert svs.shape[0] == e_eqs.shape[0]
        else:
            raise ValueError('wrong state variable array')
        return svs

    def computeGMSingleChan(self, z_mat_arg, e_eqs=None, freqs=0., svs=None,
                    w_e_eqs=None, w_freqs=None,
                    channel_name=None,
                    return_matrices=False):
        '''
        Fit the models' conductances to a given impedance matrix.

        Parameters
        ----------
        z_mat_arg: np.ndarray (ndim = 2 or 3, dtype = float or complex) or
                   list of np.ndarray (ndim = 2 or 3, dtype = float or complex)
            If a single array, represents the steady state impedance matrix,
            If a list of arrays, represents the steady state impedance
            matrices for each equilibrium potential in ``e_eqs``
        e_eqs: np.ndarray (ndim = 1, dtype = float) or float
            The equilibirum potentials in each compartment for each
            evaluation of ``z_mat``
        freqs: ``None`` or `np.array` of `complex
            Frequencies at which the impedance matrices are evaluated. If None,
            assumes that the steady state impedance matrices are provides
        channel_names: ``None`` or `list` of `string`
            The channel types to be included in the fit. If ``None``, all channel
            types that have been added to the tree are included.
        other_channel_names: ``None`` or `list` of `string`
            The channels that are not to be included in the fit
        '''
        z_mat_arg = self._preprocessZMatArg(z_mat_arg)
        e_eqs, w_e_eqs = self._preprocessEEqs(e_eqs, w_e_eqs)
        assert len(z_mat_arg) == len(e_eqs)
        freqs, w_freqs, z_mat_arg = self._preprocessFreqs(freqs, w_freqs=w_freqs, z_mat_arg=z_mat_arg)
        svs = self._preprocessExpansionPoints(svs, e_eqs)
        channel_names, other_channel_names = [channel_name], ['L']
        # do the fit
        mats_feature = []
        vecs_target = []
        for z_mat, e_eq, sv, w_e_eq in zip(z_mat_arg, e_eqs, svs, w_e_eqs):
            # set equilibrium conductances
            self.setEEq(e_eq)
            # set channel expansion point
            self.setExpansionPoints({channel_name: sv})
            # feature matrix
            g_struct = self._toStructureTensorGM(freqs=freqs, channel_names=channel_names)
            tensor_feature = np.einsum('oij,ojkl->oikl', z_mat, g_struct)
            tensor_feature *= w_freqs[:,np.newaxis,np.newaxis,np.newaxis]
            tshape = tensor_feature.shape
            mat_feature_aux = np.reshape(tensor_feature,
                                         (tshape[0]*tshape[1]*tshape[2], tshape[3]))
            # target vector
            g_mat = self.calcSystemMatrix(freqs, channel_names=other_channel_names,
                                                 indexing='tree')
            zg_prod = np.einsum('oij,ojk->oik', z_mat, g_mat)
            mat_target_aux = np.eye(len(self))[np.newaxis,:,:] - zg_prod
            mat_target_aux *= w_freqs[:,np.newaxis,np.newaxis]
            vec_target_aux = np.reshape(mat_target_aux, (tshape[0]*tshape[1]*tshape[2],))
            # store feature matrix and target vector for this voltage
            mats_feature.append(mat_feature_aux * np.sqrt(w_e_eq))
            vecs_target.append(vec_target_aux * np.sqrt(w_e_eq))
        mat_feature = np.concatenate(mats_feature)
        vec_target = np.concatenate(vecs_target)

        if return_matrices:
            return mat_feature, vec_target
        else:
            # linear regression fit
            res = la.lstsq(mat_feature, vec_target)
            g_vec = res[0].real
            # set the conductances
            self._toTreeGM(g_vec, channel_names=channel_names)

    def computeGM(self, z_mat_arg, e_eqs=None, freqs=0.,
                    w_e_eqs=None, w_freqs=None,
                    channel_names=None, other_channel_names=None,
                    return_matrices=False):
        '''
        Fit the models' conductances to a given impedance matrix.

        Parameters
        ----------
        z_mat_arg: np.ndarray (ndim = 2 or 3, dtype = float or complex) or
                   list of np.ndarray (ndim = 2 or 3, dtype = float or complex)
            If a single array, represents the steady state impedance matrix,
            If a list of arrays, represents the steady state impedance
            matrices for each equilibrium potential in ``e_eqs``
        e_eqs: np.ndarray (ndim = 1, dtype = float) or float
            The equilibirum potentials in each compartment for each
            evaluation of ``z_mat``
        freqs: ``None`` or `np.array` of `complex
            Frequencies at which the impedance matrices are evaluated. If None,
            assumes that the steady state impedance matrices are provides
        channel_names: ``None`` or `list` of `string`
            The channel types to be included in the fit. If ``None``, all channel
            types that have been added to the tree are included.
        other_channel_names: ``None`` or `list` of `string`
            The channels that are not to be included in the fit
        '''

        z_mat_arg = self._preprocessZMatArg(z_mat_arg)
        e_eqs, w_e_eqs = self._preprocessEEqs(e_eqs, w_e_eqs)
        assert len(z_mat_arg) == len(e_eqs)
        freqs, w_freqs, z_mat_arg = self._preprocessFreqs(freqs, w_freqs=w_freqs, z_mat_arg=z_mat_arg)
        if channel_names is None:
            channel_names = ['L'] + list(self.channel_storage.keys())
        if other_channel_names == None:
            other_channel_names = list(set(self.channel_storage.keys()) - set(channel_names))
        # do the fit
        mats_feature = []
        vecs_target = []
        for z_mat, e_eq, w_e_eq in zip(z_mat_arg, e_eqs, w_e_eqs):
            # set equilibrium conductances
            self.setEEq(e_eq)
            # feature matrix
            g_struct = self._toStructureTensorGM(freqs=freqs, channel_names=channel_names)
            tensor_feature = np.einsum('oij,ojkl->oikl', z_mat, g_struct)
            tensor_feature *= w_freqs[:,np.newaxis,np.newaxis,np.newaxis]
            tshape = tensor_feature.shape
            mat_feature_aux = np.reshape(tensor_feature,
                                         (tshape[0]*tshape[1]*tshape[2], tshape[3]))
            # target vector
            g_mat = self.calcSystemMatrix(freqs, channel_names=other_channel_names,
                                                 indexing='tree')
            zg_prod = np.einsum('oij,ojk->oik', z_mat, g_mat)
            mat_target_aux = np.eye(len(self))[np.newaxis,:,:] - zg_prod
            mat_target_aux *= w_freqs[:,np.newaxis,np.newaxis]
            vec_target_aux = np.reshape(mat_target_aux, (tshape[0]*tshape[1]*tshape[2],))
            # store feature matrix and target vector for this voltage
            mats_feature.append(mat_feature_aux * np.sqrt(w_e_eq))
            vecs_target.append(vec_target_aux * np.sqrt(w_e_eq))
        mat_feature = np.concatenate(mats_feature)
        vec_target = np.concatenate(vecs_target)

        if return_matrices:
            return mat_feature, vec_target
        else:
            # linear regression fit
            res = la.lstsq(mat_feature, vec_target)
            g_vec = res[0].real
            # set the conductances
            self._toTreeGM(g_vec, channel_names=channel_names)

    def _toStructureTensorGM(self, freqs, channel_names, all_channel_names=None):
        # to construct appropriate channel vector
        if all_channel_names is None:
            all_channel_names = channel_names
        else:
            assert set(channel_names).issubset(all_channel_names)
        g_vec = self._toVecGM(all_channel_names)
        g_struct = np.zeros((len(freqs), len(self), len(self), len(g_vec)), dtype=freqs.dtype)
        # fill the fit structure
        kk = 0 # counter
        for node in self:
            ii = node.index
            g_terms = node.calcMembraneConductanceTerms(freqs,
                                        channel_storage=self.channel_storage,
                                        channel_names=channel_names)
            # membrance conductance elements
            for channel_name in all_channel_names:
                if channel_name in channel_names:
                    g_struct[:,ii,ii,kk] += g_terms[channel_name]
                kk += 1
        return g_struct

    def _toVecGM(self, channel_names):
        '''
        Place all conductances to be fitted in a single vector
        '''
        g_list = []
        for node in self:
            g_list.extend([node.currents[c_name][0] for c_name in channel_names])
        return np.array(g_list)

    def _toTreeGM(self, g_vec, channel_names):
        kk = 0 # counter
        for ii, node in enumerate(self):
            for channel_name in channel_names:
                node.currents[channel_name][0] = g_vec[kk]
                kk += 1

    def computeGChanFromImpedance(self, z_mat, e_eq, freqs, weight=1.,
                                channel_names=None, all_channel_names=None, other_channel_names=None,
                                action='store'):
        # to construct appropriate channel vector
        if all_channel_names is None:
            all_channel_names = channel_names
        else:
            assert set(channel_names).issubset(all_channel_names)
        if other_channel_names is None and 'L' not in all_channel_names:
            other_channel_names = ['L']
        z_mat = self._permuteToTree(z_mat)
        if isinstance(freqs, float):
            freqs = np.array([freqs])
        # set equilibrium conductances
        self.setEEq(e_eq)
        # feature matrix
        g_struct = self._toStructureTensorGM(freqs=freqs, channel_names=channel_names,
                                             all_channel_names=all_channel_names)
        tensor_feature = np.einsum('oij,ojkl->oikl', z_mat, g_struct)
        tshape = tensor_feature.shape
        mat_feature = np.reshape(tensor_feature,
                                     (tshape[0]*tshape[1]*tshape[2], tshape[3]))
        # target vector
        g_mat = self.calcSystemMatrix(freqs, channel_names=other_channel_names,
                                             indexing='tree', add_L=False)
        zg_prod = np.einsum('oij,ojk->oik', z_mat, g_mat)
        mat_target = np.eye(len(self))[np.newaxis,:,:] - zg_prod
        vec_target = np.reshape(mat_target, (tshape[0]*tshape[1]*tshape[2],))

        return self._fitResAction(action, mat_feature, vec_target, weight,
                                  channel_names=all_channel_names)

    def computeGSingleChanFromImpedance(self, z_mat, e_eq, freqs, sv=None, weight=1.,
                                channel_name=None, all_channel_names=None, other_channel_names=None,
                                action='store'):
        # to construct appropriate channel vector
        if all_channel_names is None:
            all_channel_names = [channel_name]
        else:
            assert channel_name in all_channel_names
        if other_channel_names is None and 'L' not in all_channel_names:
            other_channel_names = ['L']
        z_mat = self._permuteToTree(z_mat)
        if isinstance(freqs, float):
            freqs = np.array([freqs])
        # set equilibrium conductances
        self.setEEq(e_eq)
        # set channel expansion point
        self.setExpansionPoints({channel_name: sv})
        # feature matrix
        g_struct = self._toStructureTensorGM(freqs=freqs, channel_names=[channel_name],
                                             all_channel_names=all_channel_names)
        tensor_feature = np.einsum('oij,ojkl->oikl', z_mat, g_struct)
        tshape = tensor_feature.shape
        mat_feature = np.reshape(tensor_feature,
                                     (tshape[0]*tshape[1]*tshape[2], tshape[3]))
        # target vector
        g_mat = self.calcSystemMatrix(freqs, channel_names=other_channel_names,
                                             indexing='tree', add_L=False)
        zg_prod = np.einsum('oij,ojk->oik', z_mat, g_mat)
        mat_target = np.eye(len(self))[np.newaxis,:,:] - zg_prod
        vec_target = np.reshape(mat_target, (tshape[0]*tshape[1]*tshape[2],))

        self.removeExpansionPoints()

        return self._fitResAction(action, mat_feature, vec_target, weight,
                                  channel_names=all_channel_names)

    def computeConcMech(self, z_mat, e_eq, freqs, ion, sv_s=None,
                        weight=1., channel_names=None, action='store'):
        np.set_printoptions(precision=5, linewidth=200)
        # print '\n', channel_names
        # print self

        if sv_s is None:
            sv_s = [None for _ in channel_names]
        exp_points = {c_name: sv for c_name, sv in zip(channel_names, sv_s)}
        self.setExpansionPoints(exp_points)

        z_mat = self._permuteToTree(z_mat)
        if isinstance(freqs, float):
            freqs = np.array([freqs])
        # set equilibrium conductances
        self.setEEq(e_eq)
        # feature matrix
        g_struct = self._toStructureTensorConc(ion, freqs, channel_names)

        # print '\nz_mat:',
        # print z_mat

        # print '\ng_struct:'
        # print g_struct

        tensor_feature = np.einsum('oij,ojkl->oikl', z_mat, g_struct)
        tshape = tensor_feature.shape
        mat_feature = np.reshape(tensor_feature,
                                     (tshape[0]*tshape[1]*tshape[2], tshape[3]))
        # target vector
        g_mat = self.calcSystemMatrix(freqs, channel_names=channel_names,
                                             indexing='tree')

        # print '\ng_mat:'
        # print g_mat

        zg_prod = np.einsum('oij,ojk->oik', z_mat, g_mat)
        mat_target = np.eye(len(self))[np.newaxis,:,:] - zg_prod
        vec_target = np.reshape(mat_target, (tshape[0]*tshape[1]*tshape[2],))

        # linear regression fit
        # res = la.lstsq(mat_feature, vec_target)
        res = so.nnls(mat_feature, vec_target)

        # print '\nmat feature: '
        # print mat_feature

        # print '\nvec feature: '
        # print vec_target
        c_vec = res[0].real
        # set the concentration mechanism parameters
        self._toTreeConc(c_vec, ion)

        print(np.set_printoptions(precision=2))
        print('z_mat fitted   =\n', self.calcImpedanceMatrix(use_conc=True, freqs=freqs, channel_names=channel_names)[0].real)

        self._toTreeConc([28.469767, 28.160618, 28.078605], ion)
        print('z_mat standard =\n', self.calcImpedanceMatrix(use_conc=True, freqs=freqs, channel_names=channel_names)[0].real)
        print('z_mat no conc  =\n', self.calcImpedanceMatrix(use_conc=False, freqs=freqs, channel_names=channel_names)[0].real)
        print('gammas =\n', c_vec)

        self.removeExpansionPoints()

        return self._fitResAction(action, mat_feature, vec_target, weight, ion=ion)

    def _toStructureTensorConc(self, ion, freqs, channel_names):
        # to construct appropriate channel vector
        c_struct = np.zeros((len(freqs), len(self), len(self), len(self)), dtype=freqs.dtype)
        # fill the fit structure
        for node in self:
            ii = node.index
            c_term = node.calcMembraneConcentrationTerms(ion, freqs=freqs,
                                    channel_names=channel_names,
                                    channel_storage=self.channel_storage)
            # print 'c_term @ node %d ='%node.index
            # print c_term
            c_struct[:,ii,ii,ii] += c_term
        return c_struct

    def _toVecConc(self, ion):
        '''
        Place concentration mechanisms to be fitted in a single vector
        '''
        return np.array([node.concmechs[ion].gamma for node in self])

    def _toTreeConc(self, c_vec, ion):
        for ii, node in enumerate(self):
            node.concmechs[ion].gamma = c_vec[ii]


    def computeC_(self, freqs, z_mat_arg, e_eqs=None, channel_names=None, w_freqs=None,):
        '''
        Fit the models' capacitances to a given impedance matrix.

        !!! This function assumes that the conductances are already fitted!!!

        Parameters
        ----------
            freqs: np.array (dtype = complex)
                Frequencies at which the impedance matrix is evaluated
            z_mat_arg: np.ndarray (ndim = 3, dtype = complex)
                The impedance matrix. The first dimension corresponds to the
                frequency, the second and third dimension contain the impedance
                matrix for that frequency
        '''
        z_mat_arg = self._preprocessZMatArg(z_mat_arg)
        if isinstance(freqs, float) or isinstance(freqs, complex):
            freqs = np.array([freqs])
        if e_eqs is None:
            e_eqs = [self.getEEq() for _ in z_mat_arg]
        elif isinstance(e_eqs, float):
            self.setEEq(e_eq)
            e_eqs = [self.getEEq() for _ in z_mat_arg]
        freqs, w_freqs, z_mat_arg = self._preprocessFreqs(freqs, w_freqs=w_freqs, z_mat_arg=z_mat_arg)
        if channel_names is None:
            channel_names = ['L'] + list(self.channel_storage.keys())
        # convert to 3d matrices if they are two dimensional
        z_mat_arg_ = []
        for z_mat in z_mat_arg:
            if z_mat.ndim == 2:
                z_mat_arg_.append(z_mat[np.newaxis,:,:])
            else:
                z_mat_arg_.append(z_mat)
            assert z_mat_arg_[-1].shape[0] == freqs.shape[0]
        # do the fit
        mats_feature = []
        vecs_target = []
        for zf_mat, e_eq in zip(z_mat_arg, e_eqs):
            # set equilibrium conductances
            self.setEEq(e_eq)
            # compute c structure tensor
            c_struct = self._toStructureTensorC(freqs)
            # feature matrix
            tensor_feature = np.einsum('oij,ojkl->oikl', zf_mat, c_struct)
            tensor_feature *= w_freqs[:,np.newaxis,np.newaxis,np.newaxis]
            tshape = tensor_feature.shape
            mat_feature_aux = np.reshape(tensor_feature, (tshape[0]*tshape[1]*tshape[2], tshape[3]))
            # target vector
            g_mat = self.calcSystemMatrix(freqs, channel_names=channel_names,
                                                 with_ca=False, indexing='tree')
            zg_prod = np.einsum('oij,ojk->oik', zf_mat, g_mat)
            mat_target = np.eye(len(self))[np.newaxis,:,:] - zg_prod
            mat_target *= w_freqs[:,np.newaxis,np.newaxis]
            vec_target_aux = np.reshape(mat_target,(tshape[0]*tshape[1]*tshape[2],))
            # store feature matrix and target vector for this voltage
            mats_feature.append(mat_feature_aux)
            vecs_target.append(vec_target_aux)
        mat_feature = np.concatenate(mats_feature, 0)
        vec_target = np.concatenate(vecs_target)
        # linear regression fit
        res = la.lstsq(mat_feature, vec_target)
        c_vec = res[0].real
        # set the capacitances
        self._toTreeC(c_vec)

    def _toStructureTensorC(self, freqs):
        c_vec = self._toVecC()
        c_struct = np.zeros((len(freqs), len(self), len(self), len(c_vec)), dtype=complex)
        for node in self:
            ii = node.index
            # capacitance elements
            c_struct[:, ii, ii, ii] += freqs
        return c_struct

    def _toVecC(self):
        return np.array([node.ca for node in self])

    def _toTreeC(self, c_vec):
        for ii, node in enumerate(self):
            node.ca = c_vec[ii]

    def computeCv2(self, taus_eig):
        # c_0 = self._permuteToTree(c_0)
        # print 'taus_m =', taus_m
        # mat_sov = self._permuteToTree(mat_sov)
        # # get the system matrix
        # mat_sys = self.calcSystemMatrix(freqs=0., channel_names=['L'],
        #                             with_ca=False, indexing='tree')
        # for ii, node in enumerate(self):
        #     # ca_node = la.lstsq(mat_sov[ii:ii+1,:].T, mat_sys[ii,:])[0][0].real
        #     # node.ca = ca_node
        #     ca_node = taus[ii] * node.currents['L'][0]
        #     node.ca = ca_node

        # c_0 = 1. / np.array([t_m * node.currents['L'][0] for t_m, node in zip(taus_m, self)])
        c_0 = np.array([node.ca for node in self])
        # construct the passive conductance matrix
        g_mat = - self.calcSystemMatrix(freqs=0., channel_names=['L'],
                                        with_ca=False, indexing='tree')
        # row matrices for capacitance fit
        g_mats = []
        for ii, node in enumerate(self):
            # g_m = np.zeros_like(g_mat)
            # g_m[ii,:] = g_mat[ii,:]
            # g_mats.append(g_m)
            g_mats.append(RowMat(g_mat[ii,:], ii))
        # construct fit functions polynomial trace fit
        self._constructEigTraceFit(g_mats, taus_eig)
        # solve by newton iteration
        c_fit = self._sn_(c_0)
        self._toTreeC(1./c_fit)
        # self._toTreeC(1./c_0)

    def _constructEigTraceFit(self, g_mats, taus_m):
        g_mats_aux = []
        for g_m in g_mats:
            gg = np.zeros((len(self), len(self)))
            gg[g_m.ii,:] = g_m.row
            g_mats_aux.append(gg)


        assert len(self) == len(g_mats)
        assert len(self) == len(taus_m)
        c_symbs = sp.symbols(['c_%d'%ii for ii in range(len(self))])
        eqs = []
        expr = sp.Float(1)
        for ii in range(len(self)):
            kk = ii+1
            eq = sp.Float(-np.sum((-1./taus_m)**kk))
            for inds in itertools.product(list(range(len(self))), repeat=kk):

                rowmat_aux = g_mats[inds[-1]]
                for jj in inds[::-1][1:]:
                    rowmat_aux = rowmat_aux.__mul__(g_mats[jj])
                # print 'row1:', rowmat_aux.row
                m_tr = rowmat_aux.trace()


                # mat_aux = np.linalg.multi_dot([g_mats_aux[jj] for jj in inds]) if len(inds) > 1 else \
                #           g_mats_aux[inds[0]]
                # print 'row2:\n', mat_aux
                # m_tr = np.trace(mat_aux)

                if np.abs(m_tr) > 1e-18:
                    print('\n >>> ', inds)
                    expr = reduce(mul, [c_symbs[jj] for jj in inds])
                    print(expr, m_tr)
                    eq += expr * m_tr
                    print(eq, '<<<\n')

            eqs.append(eq)

        jac_eqs = [[sp.diff(eq, c_s, 1) for c_s in c_symbs] for eq in eqs]

        print(eqs)
        print(jac_eqs)

        self.func = [sp.lambdify(c_symbs, eq) for eq in eqs]
        self.jac = [[sp.lambdify(c_symbs, j_eq) for j_eq in j_eqs] for j_eqs in jac_eqs]

    def _f_(self, c_arr):
        return np.array([f_i(*c_arr) for f_i in self.func])

    def _j_(self, c_arr):
        return np.array([[j_ij(*c_arr) for j_ij in js] for js in self.jac])

    def _sn_(self, c_prev, atol=1e-5, n_iter=0):
        # if n_iter < 5:
        print('__newiter__ (n = ' + str(n_iter) + '), \n-->  ca =', c_prev, '\n--> f(c) =', self._f_(c_prev))
        c_new = c_prev - np.linalg.solve(self._j_(c_prev), self._f_(c_prev))
        if np.max(np.abs(c_new - c_prev)) < atol or n_iter > 100:
            return c_new
        else:
            return self._sn_(c_new, atol=atol, n_iter=n_iter+1)

    def computeC(self, alphas, phimat, importance=None, tau_eps=5., weight=1., action='fit'):
        # np.set_printoptions(precision=2)
        n_c, n_a = len(self), len(alphas)
        assert phimat.shape == (n_a, n_c)
        if weight is None: weight = np.ones_like(alphas)
        # inds = self._permuteToTreeInds()
        # phimat = phimat[:,inds]
        # construct the passive conductance matrix
        g_mat = - self.calcSystemMatrix(freqs=0., channel_names=['L'],
                                        with_ca=False, indexing='tree')

        # ccc = 1. / np.array([nn.ca for nn in self])[:,None]

        # print '\n>>> mat 1 ='
        # print np.dot(ccc*g_mat, phimat.T).real
        # print '>>> mat 2 ='
        # print (alphas[None,:] * phimat.T).real
        # set lower limit for capacitance, fit not always well conditioned
        g_tot = np.array([node.getGTot(channel_names=['L']) for node in self])
        c_lim =  g_tot / (-alphas[0] * tau_eps)
        gamma_mat = alphas[:,None] * phimat * c_lim[None,:]

        # construct feature matrix and target vector
        mat_feature = np.zeros((n_a*n_c, n_c))
        vec_target = np.zeros(n_a*n_c)
        for ii, node in enumerate(self):
            mat_feature[ii*n_a:(ii+1)*n_a,ii] = alphas * phimat[:,ii] * weight
            # vec_target[ii*n_a:(ii+1)*n_a] = np.reshape(np.dot(phimat, g_mat[ii:ii+1,:].T), n_a) * weight**2
            vec_target[ii*n_a:(ii+1)*n_a] = np.reshape(np.dot(phimat, g_mat[ii:ii+1,:].T) - gamma_mat[:,ii:ii+1], n_a) * weight

        # least squares fit
        # res = la.lstsq(mat_feature, vec_target)[0]
        res = so.nnls(mat_feature, vec_target)[0]
        # inds = np.where(c_vec <= 0.)[0]
        # c_vec[inds] = 1e-7
        # c_vec = np.abs(c_vec)
        c_vec = res + c_lim
        self._toTreeC(c_vec)

        # ccc = 1. / c_vec[:,None]
        # print '\n>>> mat 1 ='
        # print np.dot(ccc*g_mat, phimat.T).real
        # print '>>> mat 2 ='
        # print (alphas[None,:] * phimat.T).real
        # return self._fitResAction(action, mat_feature, vec_target, weight,
        #                           capacitance=True)

    def computeCVF(self, freqs, zf_mat, eps=.05, max_iter=20, action='store'):
        fef = ke.fExpFitter()
        n_c = len(self)
        # reshape Zmat for vector fitting
        zf_mat = self._permuteToTree(zf_mat)
        zf_mat = np.reshape(zf_mat, (len(freqs),zf_mat.shape[1]*zf_mat.shape[2]))
        # perform vector fit
        alpha, gammas, pairs, rms = fef.fitFExp_vector(freqs, zf_mat.T, deg=n_c)
        # print '\n>>>>>'
        # print 'taus together = ', 1e3 / alpha
        # print '<<<<<\n'

        # compute capacitances first approx
        # gammas = np.reshape(gammas, (n_c, n_c, n_c))
        # gammas_ = np.sum(gammas, axis=0)
        # c1 = 1. / np.diag(gammas_).real

        taulist = []

        # from datarep.matplotlibsettings import *
        for ii, gamma in enumerate(gammas):
            kk, ll = ii // n_c, ii % n_c

            alpha_, gamma_, pair, rms = fef.fitFExp(freqs, zf_mat[:,ii], deg=n_c)
            taus_ = 1e3 / alpha_
            # print 'taus separate = ', taus_
            taulist.extend(taus_.real.tolist())
            kf_ = fef.sumFExp(freqs, alpha_, gamma_)


            # kf = fef.sumFExp(freqs, alpha, gamma)
            # pl.figure('kf %d <-> %d'%(kk,ll))
            # pl.plot(freqs.imag, zf_mat[:,ii].real, c=colours[0])
            # pl.plot(freqs.imag, zf_mat[:,ii].imag, c=colours[1])
            # # pl.plot(freqs.imag, kf.real, ls='--', lw=1.6, c=colours[0])
            # # pl.plot(freqs.imag, kf.imag, ls='--', lw=1.6, c=colours[1])
            # pl.plot(freqs.imag, kf_.real, ls='-.', lw=1.6, c=colours[0])
            # pl.plot(freqs.imag, kf_.imag, ls='-.', lw=1.6, c=colours[1])

        from scipy.cluster.vq import kmeans
        t_init = np.logspace(np.log10(np.min(taulist)), np.log10(np.max(taulist)), n_c)
        # print t_init
        logtau_all, _ = kmeans(np.log10(np.array(taulist)[:,None]), np.log10(t_init[:,None]))
        tau_all = np.power(10., logtau_all)
        # print tau_all

        alpha = 1e3 / tau_all.reshape(tau_all.shape[0])
        pairs = np.zeros_like(alpha, dtype=bool)

        gammas = np.zeros((n_c, n_c, n_c))

        for ii, zf in enumerate(zf_mat.T):
            kk, ll = ii // n_c, ii % n_c

            # print freqs.shape, zf.shape, alpha.shape, pair.ahsp

            gamma = fef.fit_residues(freqs, zf, alpha, pair)
            kf_ = fef.sumFExp(freqs, alpha, gamma)
            gammas[:,kk,ll] = gamma.real

            pl.figure('kf %d <-> %d'%(kk,ll))
            pl.plot(freqs.imag, zf_mat[:,ii].real, c=colours[0])
            pl.plot(freqs.imag, zf_mat[:,ii].imag, c=colours[1])
            # pl.plot(freqs.imag, kf.real, ls='--', lw=1.6, c=colours[0])
            # pl.plot(freqs.imag, kf.imag, ls='--', lw=1.6, c=colours[1])
            pl.plot(freqs.imag, kf_.real, ls='-.', lw=1.6, c=colours[0])
            pl.plot(freqs.imag, kf_.imag, ls='-.', lw=1.6, c=colours[1])

        # pl.figure()
        # ax = pl.gca()
        # ax.hist(taulist, bins=np.logspace(-4,2,100))
        # for tt in tau_all:
        #     ax.axvline(tt, color='r')
        # for tt in t_init:
        #     ax.axvline(tt, color='b')
        # ax.set_xscale('log')

        # # pl.show()
        # np.set_printoptions(precision=2, edgeitems=10, linewidth=500, suppress=True)
        # gammas_ = np.sum(gammas, axis=0)
        # print gammas_
        # np.set_printoptions(precision=8, edgeitems=3, linewidth=75, suppress=False)
        # c1 = 1. / np.diag(gammas_).real

        # # compute the matrix of the dynamical system
        # g_mat = self.calcSystemMatrix(freqs=0., channel_names=['L'],
        #                                 with_ca=False, indexing='tree')
        # inds_zero = np.where(np.abs(g_mat) < 1e-16)
        # ca_vec = np.array([node.ca for node in self])
        # gc_mat = g_mat / ca_vec[:,None]
        # np.set_printoptions(precision=4, edgeitems=10, linewidth=500, suppress=False)
        # print '-- ca_vec original = ', ca_vec
        # # algorithm iteration
        # ca_diff = np.ones_like(ca_vec)
        # kk = 0
        # while np.mean(ca_diff) > eps and kk < max_iter:
        #     print '\n>> iter no. %d'%kk
        #     print '>> gc_mat_orig =\n', gc_mat
        #     # compute Schur decomposition of the matrix
        #     triang, umat = la.schur(gc_mat, output='complex')
        #     triang_diag = np.diag(triang)
        #     print '>> triang =\n', triang
        #     ll,_ = np.linalg.eig(gc_mat)
        #     print '>> eig orig =\n', ll
        #     print '>> alphas =\n', alpha
        #     # assing alphas to closest triang diag elements
        #     inds = np.argsort(triang_diag)
        #     np.fill_diagonal(triang, alpha[inds])
        #     # construct closest matrix with given eigenvalues
        #     gc_mat = np.dot(umat, np.dot(triang, np.conjugate(umat.T)))

        #     triang, umat = la.schur(gc_mat, output='complex')
        #     print '>> triang_new =\n', triang
        #     ll, vv = np.linalg.eig(gc_mat)
        #     print '>> eig new nozeros =\n', ll

        #     print '>> gc new nozeros =\n', gc_mat
        #     # construct closest system matrix
        #     gc_mat[inds_zero] = 0.
        #     ll, vv = np.linalg.eig(gc_mat)
        #     print '>> eig new =\n', ll
        #     print '>> gc new zeros =\n', gc_mat
        #     # extract capacitances
        #     ca_vec_ = np.sum(g_mat, axis=1) / np.sum(gc_mat, axis=1)

        #     print '>> g ca difference =\n', g_mat - gc_mat * ca_vec_[:,None]

        #     # continuation conditions
        #     kk += 1
        #     ca_diff = np.abs(ca_vec - ca_vec_) / ca_vec
        #     ca_vec = ca_vec_


        # g_mat = self.calcSystemMatrix(freqs=0., channel_names=['L'],
        #                                 with_ca=False, indexing='tree')
        # ca_vec = np.array([node.ca for node in self])

        # #     print '   ca_vec = ', ca_vec
        # # a_orig, _, _ = self.calcEigenvalues()
        # eig_orig, _ = la.eig(g_mat / ca_vec[:,None])
        # print '\n>> ca_orig =\n', ca_vec
        # print '>> a_orig =\n', np.sort(eig_orig)[::-1]

        # print '\n>> a_target =\n', alpha#*1e7

        # # compute the matrix of the dynamical system
        # # create the matrix pencil
        # pencil = np.array([np.zeros_like(g_mat) for _ in range(len(self)+1)])
        # for ii in range(len(self)):
        #     pencil[ii+1,ii,:] = g_mat[ii]
        # # create IEP solver
        # from neat.tools.fittools import iepsolver
        # ieps = iepsolver.IEPSolver(pencil)
        # ieps.initLambdas(alpha[-2:-1])

        # ppp = ieps.evalPencil(1./ca_vec)
        # eig_orig, _ = la.eig(ppp)
        # print '>> a_orig 2 =\n', np.sort(eig_orig)[::-1]
        # # fit the capacitances
        # c_fit, r_fit = ieps.minimizeResiduals(1./ca_vec, pprint=True)
        # ca_new = 1. / c_fit

        # eig_new, _ = la.eig(g_mat / ca_new[:,None])
        # print '\n>> ca_new =\n', ca_new
        # print '>> a_new =\n', np.sort(eig_new)[::-1]



        # pl.show()

        # c
        # self._toTreeC(ca_new)

    def computeGC(self, freqs, zf_mat, z_mat=None):
        '''
        Fit the models' conductances and capacitances to a given impedance matrix
        evaluated at a number of frequency points in the Fourrier domain.

        Parameters
        ----------
            freqs: np.array (dtype = complex)
                Frequencies at which the impedance matrix is evaluated
            zf_mat: np.ndarray (ndim = 3, dtype = complex)
                The impedance matrix. The first dimension corresponds to the
                frequency, the second and third dimension contain the impedance
                matrix for that frequency
            z_mat:  np.ndarray (ndim = 2, dtype = float) or None (default)
                The steady state impedance matrix. If ``None`` is given, the
                function tries to find index of freq = 0 in ``freqs`` to
                determine ``z_mat``. If no such element is found, a
                ``ValueError`` is raised

        Raises
        ------
            ValueError: if no freq = 0 is found in ``freqs`` and no steady state
                impedance matrix is given
        '''
        if z_mat is None:
            try:
                ind0 = np.where(np.abs(freqs) < 1e-12)[0]
                z_mat = zf_mat[ind0,:,:].real
            except IndexError:
                raise ValueError("No zero frequency in `freqs`")
        # compute leak and coupling conductances
        self.computeG(z_mat)
        # compute capacitances
        self.computeC(freqs, zf_mat)

    # def computeGC_(self, freqs, zf_mat):
    #     '''
    #     Trial to fit the models' conductances and capacitances at once.
    #     So far unsuccesful.
    #     '''
    #     gc_struct = self._toStructureTensorGC(freqs)
    #     # fitting matrix for linear model
    #     tensor_feature = np.einsum('oij,ojkl->oikl', zf_mat, gc_struct)
    #     tshape = tensor_feature.shape
    #     mat_feature = np.reshape(tensor_feature,
    #                              (tshape[0]*tshape[1]*tshape[2], tshape[3]))
    #     vec_target = np.reshape(np.array([np.eye(len(self), dtype=complex) for _ in freqs]),
    #                             (len(self)*len(self)*len(freqs),))
    #     # linear regression fit
    #     res = la.lstsq(mat_feature, vec_target)
    #     gc_vec = res[0].real
    #     # set conductances and capacitances
    #     self._toTreeGC(gc_vec)

    # def _toStructureTensorGC(self, freqs):
    #     gc_vec = self._toVecGC()
    #     gc_struct = np.zeros((len(freqs), len(self), len(self), len(gc_vec)), dtype=complex)
    #     for node in self:
    #         ii = node.index
    #         if node.parent_node == None:
    #             # leak conductance elements
    #             gc_struct[:, 0, 0, 0] += 1
    #             # capacitance elements
    #             gc_struct[:, 0, 0, 0] += freqs
    #         else:
    #             kk = 3 * node.index - 1
    #             jj = node.parent_node.index
    #             # coupling conductance elements
    #             gc_struct[:, ii, jj, kk] -= 1.
    #             gc_struct[:, jj, ii, kk] -= 1.
    #             gc_struct[:, jj, jj, kk] += 1.
    #             gc_struct[:, ii, ii, kk] += 1.
    #             # leak conductance elements
    #             gc_struct[:, ii, ii, kk+1] += 1.
    #             # capacitance elements
    #             gc_struct[:, ii, ii, kk+2] += freqs
    #     return gc_struct

    # def _toVecGC(self):
    #     gc_list = []
    #     for node in self:
    #         if node.parent_node is None:
    #             gc_list.extend([node.currents['L'][0], node.ca])
    #         else:
    #             gc_list.extend([node.g_c, node.currents['L'][0], node.ca])
    #     return np.array(gc_list)

    # def _toTreeGC(self, gc_vec):
    #     for ii, node in enumerate(self):
    #         if node.parent_node is None:
    #             node.currents['L'][0] = gc_vec[ii]
    #             node.ca  = gc_vec[ii+1]
    #         else:
    #             node.g_c = gc_vec[3*ii-2]
    #             node.currents['L'][0] = gc_vec[3*ii-1]
    #             node.ca  = gc_vec[3*ii]

    # def computeGChan(self, v_mat, i_mat,
    #                  p_open_channels=None, p_open_other_channels=None):
    #     '''
    #     Parameters
    #     ----------
    #     v_mat: np.ndarray (n,k)
    #     i_mat: np.ndarray (n,k)
    #         n = nr. of locations, k = nr. of fit points
    #     '''
    #     channel_names = p_open_channels.keys()
    #     # check size
    #     assert v_mat.shape == i_mat.shape
    #     assert v_mat.shape[0] == len(self)
    #     n_loc, n_fp, n_chan = len(self), i_mat.shape[1], len(channel_names)
    #     # create lin fit arrays
    #     i_vec = np.zeros((n_loc, n_fp))
    #     d_vec = np.zeros((n_loc, n_fp, n_chan))
    #     # iterate over number of fit points
    #     for jj, (i_, v_) in enumerate(zip(i_mat.T, v_mat.T)):
    #         i_aux, d_aux = self._toVecGChan(i_, v_,
    #                                         p_open_channels=p_open_channels,
    #                                         p_open_other_channels=p_open_other_channels)
    #         i_vec[:,jj] = i_aux
    #         d_vec[:,jj,:] = d_aux

    #     # iterate over locations:
    #     g_chan = np.zeros((n_loc, n_fp))
    #     for ll, (i_, d_) in enumerate(zip(i_vec, d_vec)):
    #         node = self[ll]
    #         # conductance fit at node ll
    #         g_ = la.lstsq(d_, i_)[0]
    #         # store the conductances
    #         for ii, channel_name in enumerate(channel_names):
    #             node.currents[channel_name][0] = g_[ii]

    def computeGChanFromTrace(self, dv_mat, v_mat, i_mat,
                         p_open_channels=None, p_open_other_channels={}, test={},
                         weight=1.,
                         channel_names=None, all_channel_names=None, other_channel_names=None,
                         action='store'):
        '''
        Assumes leak conductance, coupling conductance and capacitance have
        already been fitted

        Parameters
        ----------
        dv_mat: np.ndarray (n,k)
        v_mat: np.ndarray (n,k)
        i_mat: np.ndarray (n,k)
            n = nr. of locations, k = nr. of fit points
        '''
        # print '\nxxxxxxxx'
        # check size
        assert v_mat.shape == i_mat.shape
        assert dv_mat.shape == i_mat.shape
        assert dv_mat.shape == i_mat.shape
        for channel_name, p_open in p_open_channels.items():
            assert p_open.shape == i_mat.shape
        for channel_name, p_open in p_open_other_channels.items():
            assert p_open.shape == i_mat.shape

        # define channel name lists
        if channel_names is None:
            channel_names = list(p_open_channels.keys())
        else:
            assert set(channel_names) == set(p_open_channels.keys())
        if other_channel_names is None:
            other_channel_names = list(p_open_other_channels.keys())
        else:
            assert set(other_channel_names) == set(p_open_other_channels.keys())
        if all_channel_names == None:
            all_channel_names = channel_names
        else:
            assert set(channel_names).issubset(all_channel_names)

        # numbers for fit
        n_loc, n_fp, n_chan = len(self), i_mat.shape[1], len(all_channel_names)

        # import matplotlib.pyplot as pl
        # from datarep.matplotlibsettings import *
        # t_arr = np.arange(n_fp)

        mat_feature = np.zeros((n_fp * n_loc, n_loc * n_chan))
        vec_target = np.zeros(n_fp * n_loc)
        for ii, node in enumerate(self):
            # define fit vectors
            i_vec = np.zeros(n_fp)
            d_vec = np.zeros((n_fp, n_chan))
            # add input current
            i_vec += i_mat[node.loc_ind]
            # add capacitive current
            i_vec -= node.ca * dv_mat[node.loc_ind] * 1e3 # convert to nA
            # add the coupling terms
            pnode = node.parent_node
            if pnode is not None:
                i_vec += node.g_c * (v_mat[pnode.loc_ind] - v_mat[node.loc_ind])
            for cnode in node.child_nodes:
                i_vec += cnode.g_c * (v_mat[cnode.loc_ind] - v_mat[node.loc_ind])
            # add the leak terms
            g_l, e_l = node.currents['L']
            i_vec += g_l * (e_l - v_mat[node.loc_ind])
            # add the ion channel current
            for channel_name, p_open in p_open_other_channels.items():
                i_vec -= node.getDynamicI(channel_name,
                                        p_open[node.loc_ind], v_mat[node.loc_ind])
            # drive terms
            for kk, channel_name in enumerate(all_channel_names):
                if channel_name in channel_names:
                    p_open = p_open_channels[channel_name]
                    d_vec[:, kk] = node.getDynamicDrive(channel_name,
                                            p_open[node.loc_ind], v_mat[node.loc_ind])

            # # do the fit
            # g_chan = la.lstsq(d_vec, i_vec)[0]
            # print 'g_chan =', g_chan
            # for kk, channel_name in enumerate(all_channel_names):
            #     mat_feature[ii*n_fp:(ii+1)*n_fp, ii*n_chan+kk] = d_vec[:,kk]
            #     vec_target[ii*n_fp:(ii+1)*n_fp] = i_vec


            # pl.figure('i @ ' + str(node), figsize=(10,5))
            # ax = pl.subplot(121)
            # ax.set_title('sum currents')
            # ax.plot(t_arr, i_vec, 'b')
            # # ax.plot(test['t'], test['iin'] + test['ic'] + test['il'], 'b--', lw=2)

            # # pl.plot(t_arr, d_vec[:,0], 'r')
            # ax.plot(t_arr, np.dot(d_vec, g_chan), 'g--')

            # ax = pl.subplot(122)
            # ax.set_title('individual currents')
            # ax.plot(t_arr, i_mat[node.loc_ind], c=colours[0], label=r'$I_{in}$')
            # # ax.plot(test['t'], test['iin'], c=colours[0], ls='--', lw=2)

            # ax.plot(t_arr, - node.ca * dv_mat[node.loc_ind] * 1e3, c=colours[1], label=r'$I_{cap}$')
            # # ax.plot(test['t'], test['ic'], c=colours[1], ls='--', lw=2)

            # ax.plot(t_arr, g_l * (e_l - v_mat[node.loc_ind]), c=colours[2], label=r'$I_{leak}$')
            # # ax.plot(test['t'], test['il'], c=colours[2], ls='--', lw=2)

            # if pnode is not None:
            #     ax.plot(t_arr, node.g_c * (v_mat[pnode.loc_ind] - v_mat[node.loc_ind]), c=colours[3], label=r'$I_{c parent}$')
            # for ii, cnode in enumerate(node.child_nodes):
            #     ax.plot(t_arr, cnode.g_c * (v_mat[cnode.loc_ind] - v_mat[node.loc_ind]), c=colours[(4+ii)%len(colours)], label=r'$I_{c child}$')
            # ax.legend(loc=0)


        # pl.show()

        return self._fitResAction(action, mat_feature, vec_target, weight,
                                  channel_names=all_channel_names)

    def computeGChanFromTraceConv(self, dt, v_mat, i_mat,
                         p_open_channels=None, p_open_other_channels={}, test={},
                         weight=1.,
                         channel_names=None, all_channel_names=None, other_channel_names=None,
                         v_pas=None,
                         action='store'):
        '''
        Assumes leak conductance, coupling conductance and capacitance have
        already been fitted

        Parameters
        ----------
        dv_mat: np.ndarray (n,k)
        v_mat: np.ndarray (n,k)
        i_mat: np.ndarray (n,k)
            n = nr. of locations, k = nr. of fit points
        '''


        import matplotlib.pyplot as pl
        # from datarep.matplotlibsettings import *
        # print '\nxxxxxxxx'
        # check size
        assert v_mat.shape == i_mat.shape
        for channel_name, p_open in p_open_channels.items():
            assert p_open.shape == i_mat.shape
        for channel_name, p_open in p_open_other_channels.items():
            assert p_open.shape == i_mat.shape

        # define channel name lists
        if channel_names is None:
            channel_names = list(p_open_channels.keys())
        else:
            assert set(channel_names) == set(p_open_channels.keys())
        if other_channel_names is None:
            other_channel_names = list(p_open_other_channels.keys())
        else:
            assert set(other_channel_names) == set(p_open_other_channels.keys())
        if all_channel_names == None:
            all_channel_names = channel_names
        else:
            assert set(channel_names).issubset(all_channel_names)

        es_eq = np.array([node.e_eq for node in self])

        # permute inputs to tree
        perm_inds = self._permuteToTreeInds()
        v_mat = v_mat[perm_inds,:]
        i_mat = i_mat[perm_inds,:]
        for p_o in p_open_channels.values():
            p_o = p_o[perm_inds,:]
        for p_o in p_open_other_channels.values():
            p_o = p_o[perm_inds,:]

        # numbers for fit
        n_loc, n_fp, n_chan = len(self), i_mat.shape[1], len(all_channel_names)

        # import matplotlib.pyplot as pl
        # from datarep.matplotlibsettings import *
        # pl.figure('v conv', figsize=(25,6))
        # ax = pl.subplot(161)
        # ax.set_title('v soma')
        # ax_ = pl.subplot(164)
        # ax_.set_title('v dend')

        if v_pas is None:
            # compute convolution input current for fit
            for channel_name, p_open in p_open_other_channels.items():
                for ii, node in enumerate(self):
                    i_mat[ii] -= node.getDynamicI(channel_name, p_open[ii], v_mat[ii])
            v_i_in = self._calcConvolution(dt, i_mat[:,np.newaxis,:])
            v_i_in = np.sum(v_i_in[:,:,0,:], axis=1)
            v_fit = v_mat - es_eq[:,None] - v_i_in
        else:
            v_fit = v_mat - es_eq[:,None] - v_pas
        v_fit_aux = v_fit

        # if v_pas is None:
        #     ax.plot(t_arr, v_i_in[0], 'b', label='v in')
        # else:
        #     ax.plot(t_arr, v_pas[0], 'b', label='v pas')
        # ax.plot(t_arr, v_mat[0] - es_eq[0], 'r', label='v real')
        # ax.plot(t_arr, v_fit[0], 'y', label='v tofit')

        # if v_pas is None:
        #     ax_.plot(t_arr, v_i_in[1], 'b', label='v in')
        # else:
        #     ax_.plot(t_arr, v_pas[1], 'b', label='v pas')
        # ax_.plot(t_arr, v_mat[1] - es_eq[1], 'r', label='v real')
        # ax_.plot(t_arr, v_fit[1], 'y', label='v tofit')

        v_fit = np.reshape(v_fit, n_loc*n_fp)


        # compute channel drive convolutions
        d_chan = np.zeros((n_loc, n_chan, n_fp))
        for kk, channel_name in enumerate(all_channel_names):
            if channel_name in channel_names:
                p_open = p_open_channels[channel_name]
                for ii, node in enumerate(self):
                    # d_chan[ii,kk,:] -= node.getDynamicDrive(channel_name, p_open[ii], v_mat[ii])
                    d_chan[ii,kk,:] -= node.getDynamicDrive_(channel_name, v_mat[ii], dt, channel_storage=self.channel_storage)
        v_d = self._calcConvolution(dt, d_chan)
        v_d_aux = v_d


        # ax_d = pl.subplot(162)
        # ax_d.set_title('v_drive soma')
        # ax_d.plot(t_arr, v_d_aux[0,0,0,:], label='from soma')
        # ax_d.plot(t_arr, v_d_aux[0,1,0,:], label='from dend')
        # ax_d.legend(loc=0)
        # ax_d = pl.subplot(163)
        # ax_d.set_title('drive soma')
        # ax_d.plot(t_arr, d_chan[0,0,:])

        # ax_d = pl.subplot(165)
        # ax_d.set_title('v_drive dend')
        # ax_d.plot(t_arr, v_d_aux[1,0,0,:], label='from soma')
        # ax_d.plot(t_arr, v_d_aux[1,1,0,:], label='from dend')
        # ax_d.legend(loc=0)
        # ax_d = pl.subplot(166)
        # ax_d.set_title('drive dend')
        # ax_d.plot(t_arr, d_chan[1,0,:])


        v_d = np.reshape(v_d, (n_loc, n_loc*n_chan, n_fp))
        v_d = np.moveaxis(v_d, -1, 1)
        v_d = np.reshape(v_d, (n_loc * n_fp, n_loc*n_chan))
        # create the matrices for fit
        mat_feature = v_d
        vec_target = v_fit

        # g_vec = la.lstsq(mat_feature, vec_target)[0]
        g_vec = so.nnls(mat_feature, vec_target)[0]
        # ax.plot(t_arr, g_vec[0]*v_d_aux[0,0,0,:] + g_vec[1]*v_d_aux[0,1,0,:], 'g--', label='v chanfit')
        # ax.legend(loc=0)
        # ax_.plot(t_arr, g_vec[0]*v_d_aux[1,0,0,:] + g_vec[1]*v_d_aux[1,0,0,:], 'g--', label='v chanfit')
        # ax_.legend(loc=0)
        print('g single fit =', g_vec)

        n_panel = len(self)+1
        t_arr = np.arange(n_fp) * dt

        pl.figure('fit', figsize=(n_panel*3, n_panel*3))
        gs = pl.GridSpec(n_panel,n_panel)
        gs.update(top=0.98, bottom=0.05, left=0.05, right=0.98, hspace=0.4, wspace=0.4)

        for ii, node in enumerate(self):
            # plot voltage
            ax_v = myAx(pl.subplot(gs[ii+1,0]))
            ax_v.set_title('node %d'%node.index)
            ax_v.plot(t_arr, v_mat[ii] - es_eq[ii], c='r', label=r'$V_{rec}$')
            ax_v.plot(t_arr, v_i_in[ii], c='b', label=r'$V_{inp}$')
            ax_v.plot(t_arr, v_fit_aux[ii], c='y', label=r'$V_{tofit}$')
            # compute fitted voltage
            v_fitted = np.zeros_like(t_arr)
            for jj in range(n_loc):
                for kk in range(n_chan):
                    v_fitted += g_vec[jj*n_chan+kk] * v_d_aux[ii,jj,kk]
            ax_v.plot(t_arr, v_fitted, c='c', ls='--', lw=1.6, label=r'$V_{fitted}$')
            ax_v.set_xlabel(r'$t$ (ms)')
            ax_v.set_ylabel(r'$V$ (mV)')
            myLegend(ax_v, loc='upper left')

            # plot drive
            ax_d = myAx(pl.subplot(gs[0,ii+1]))
            ax_d.set_title('node %d'%node.index)
            for kk, cname in enumerate(channel_names):
                ax_d.plot(t_arr, d_chan[ii,kk], c=colours[kk%len(colours)], label=cname)
            ax_d.set_xlabel(r'$t$ (ms)')
            ax_d.set_ylabel(r'$D$ (mV)')
            myLegend(ax_d, loc='upper left')

            for jj, node in enumerate(self):
                # plot drive convolution
                ax_vd = myAx(pl.subplot(gs[ii+1,jj+1]))
                for kk, cname in enumerate(channel_names):
                    ax_vd.plot(t_arr, g_vec[jj*n_chan+kk] * v_d_aux[ii,jj,kk], c=colours[kk%len(colours)], label=cname)
                ax_vd.set_xlabel(r'$t$ (ms)')
                ax_vd.set_ylabel(r'$C$ (mV)')
                myLegend(ax_vd, loc='upper left')



        # import matplotlib.pyplot as pl
        # from datarep.matplotlibsettings import *
        # t_arr = np.arange(n_fp)

        # mat_feature = np.zeros((n_fp * n_loc, n_loc * n_chan))
        # vec_target = np.zeros(n_fp * n_loc)
        # for ii, node in enumerate(self):
        #     # define fit vectors
        #     i_vec = np.zeros(n_fp)
        #     d_vec = np.zeros((n_fp, n_chan))
        #     # add input current
        #     i_vec += i_mat[node.loc_ind]
        #     # add capacitive current
        #     i_vec -= node.ca * dv_mat[node.loc_ind] * 1e3 # convert to nA
        #     # add the coupling terms
        #     pnode = node.parent_node
        #     if pnode is not None:
        #         i_vec += node.g_c * (v_mat[pnode.loc_ind] - v_mat[node.loc_ind])
        #     for cnode in node.child_nodes:
        #         i_vec += cnode.g_c * (v_mat[cnode.loc_ind] - v_mat[node.loc_ind])
        #     # add the leak terms
        #     g_l, e_l = node.currents['L']
        #     i_vec += g_l * (e_l - v_mat[node.loc_ind])
        #     # add the ion channel current
        #     for channel_name, p_open in p_open_other_channels.iteritems():
        #         i_vec -= node.getDynamicI(channel_name,
        #                                 p_open[node.loc_ind], v_mat[node.loc_ind])
        #     # drive terms
        #     for kk, channel_name in enumerate(all_channel_names):
        #         if channel_name in channel_names:
        #             p_open = p_open_channels[channel_name]
        #             d_vec[:, kk] = node.getDynamicDrive(channel_name,
        #                                     p_open[node.loc_ind], v_mat[node.loc_ind])

        #     # do the fit
        #     g_chan = la.lstsq(d_vec, i_vec)[0]
        #     print 'g_chan =', g_chan
        #     for kk, channel_name in enumerate(all_channel_names):
        #         mat_feature[ii*n_fp:(ii+1)*n_fp, ii*n_chan+kk] = d_vec[:,kk]
        #         vec_target[ii*n_fp:(ii+1)*n_fp] = i_vec


        #     pl.figure('i @ ' + str(node), figsize=(10,5))
        #     ax = pl.subplot(121)
        #     ax.set_title('sum currents')
        #     ax.plot(t_arr, i_vec, 'b')
        #     # ax.plot(test['t'], test['iin'] + test['ic'] + test['il'], 'b--', lw=2)

        #     # pl.plot(t_arr, d_vec[:,0], 'r')
        #     ax.plot(t_arr, np.dot(d_vec, g_chan), 'g--')

        #     ax = pl.subplot(122)
        #     ax.set_title('individual currents')
        #     ax.plot(t_arr, i_mat[node.loc_ind], c=colours[0], label=r'$I_{in}$')
        #     # ax.plot(test['t'], test['iin'], c=colours[0], ls='--', lw=2)

        #     ax.plot(t_arr, - node.ca * dv_mat[node.loc_ind] * 1e3, c=colours[1], label=r'$I_{cap}$')
        #     # ax.plot(test['t'], test['ic'], c=colours[1], ls='--', lw=2)

        #     ax.plot(t_arr, g_l * (e_l - v_mat[node.loc_ind]), c=colours[2], label=r'$I_{leak}$')
        #     # ax.plot(test['t'], test['il'], c=colours[2], ls='--', lw=2)

        #     if pnode is not None:
        #         ax.plot(t_arr, node.g_c * (v_mat[pnode.loc_ind] - v_mat[node.loc_ind]), c=colours[3], label=r'$I_{c parent}$')
        #     for ii, cnode in enumerate(node.child_nodes):
        #         ax.plot(t_arr, cnode.g_c * (v_mat[cnode.loc_ind] - v_mat[node.loc_ind]), c=colours[(4+ii)%len(colours)], label=r'$I_{c child}$')
        #     ax.legend(loc=0)

        # pl.tight_layout()
        pl.show()

        return self._fitResAction(action, mat_feature, vec_target, weight,
                                  channel_names=all_channel_names)

    # def _fitResAction(self, action, mat_feature, vec_target, weight, channel_names):
    #     if action == 'fit':
    #         # linear regression fit
    #         # res = la.lstsq(mat_feature, vec_target)
    #         res = so.nnls(mat_feature, vec_target)
    #         g_vec = res[0].real
    #         # set the conductances
    #         self._toTreeGM(g_vec, channel_names=channel_names)
    #     elif action == 'return':
    #         return mat_feature, vec_target
    #     elif action == 'store':
    #         if len(self.fit_data['channel_names']) == 0:
    #             self.fit_data['channel_names'] = channel_names
    #         else:
    #             try:
    #                 assert self.fit_data['channel_names'] == channel_names
    #             except AssertionError:
    #                 print str(channel_names)
    #                 print str(self.fit_data['channel_names'])
    #                 raise IOError('`channel_names` does not agree with stored ' + \
    #                               'channel names for other fits\n' + \
    #                               '`channel_names`:      ' + str(channel_names) + \
    #                               '\nstored channel names: ' + str(self.fit_data['channel_names']))
    #         self.fit_data['mats_feature'].append(mat_feature)
    #         self.fit_data['vecs_target'].append(vec_target)
    #         self.fit_data['weights_fit'].append(weight)
    #     else:
    #         raise IOError('Undefined action, choose \'fit\', \'return\' or \'store\'.')

    def _fitResAction(self, action, mat_feature, vec_target, weight,
                            capacitance=False, **kwargs):
        if action == 'fit':
            # linear regression fit
            # res = la.lstsq(mat_feature, vec_target)
            res = so.nnls(mat_feature, vec_target)
            vec_res = res[0].real
            # set the conductances
            if 'channel_names' in kwargs:
                self._toTreeGM(vec_res, channel_names=kwargs['channel_names'])
            elif 'ion' in kwargs:
                self._toTreeConc(vec_res, kwargs['ion'])
            elif capacitance:
                self._toTreeC(vec_res)
            else:
                raise IOError('Provide \'channel_names\' or \'ion\' as keyword argument, ' + \
                              'or set \'capacitance\' to `True`')
        elif action == 'return':
            return mat_feature, vec_target
        elif action == 'store':
            if 'channel_names' in kwargs:
                try:
                    assert self.fit_data['ion'] == ''
                except AssertionError:
                    raise IOError('Stored fit matrices are concentration mech fits, ' + \
                                  'do not try to store channel conductance fit matrices')
                if len(self.fit_data['channel_names']) == 0:
                    self.fit_data['channel_names'] = kwargs['channel_names']
                else:
                    try:
                        assert self.fit_data['channel_names'] == kwargs['channel_names']
                    except AssertionError:
                        raise IOError('`channel_names` does not agree with stored ' + \
                                      'channel names for other fits\n' + \
                                      '`channel_names`:      ' + str(kwargs['channel_names']) + \
                                      '\nstored channel names: ' + str(self.fit_data['channel_names']))
            elif 'ion' in kwargs:
                try:
                    assert len(self.fit_data['channel_names']) == 0
                except AssertionError:
                    raise IOError('Stored fit matrices are channel conductance fits, ' + \
                                  'do not try to store concentration fit matrices')
                if self.fit_data['ion'] == '':
                    self.fit_data['ion'] = kwargs['ion']
                else:
                    try:
                        assert self.fit_data['ion'] == kwargs['ion']
                    except AssertionError:
                        raise IOError('`ion` does not agree with stored ion for ' + \
                                      'other fits:\n' + \
                                      '`ion`: ' + kwargs[ion] + \
                                      '\nstored ion: ' + self.fit_data['ion'])
            elif capacitance:
                self.fit_data['c'] = True

            self.fit_data['mats_feature'].append(mat_feature)
            self.fit_data['vecs_target'].append(vec_target)
            self.fit_data['weights_fit'].append(weight)
        else:
            raise IOError('Undefined action, choose \'fit\', \'return\' or \'store\'.')

    def resetFitData(self):
        self.fit_data = dict(mats_feature=[],
                             vecs_target=[],
                             weights_fit=[],
                             channel_names=[],
                             ion='',
                             c=False)

    def runFit(self):
        fit_data = self.fit_data
        if len(fit_data['mats_feature']) > 0:
            # apply the weights
            for (m_f, v_t, w_f) in zip(fit_data['mats_feature'], fit_data['vecs_target'], fit_data['weights_fit']):
                nn = len(v_t)
                m_f *= w_f / nn
                v_t *= w_f / nn
            # create the fit matrices
            mat_feature = np.concatenate(fit_data['mats_feature'])
            vec_target = np.concatenate(fit_data['vecs_target'])
            # do the fit
            if len(fit_data['channel_names']) > 0:
                self._fitResAction('fit', mat_feature, vec_target, 1.,
                                   channel_names=fit_data['channel_names'])
            elif fit_data['ion'] != '':
                self._fitResAction('fit', mat_feature, vec_target, 1.,
                                   ion=fit_data['ion'])
            elif fit_data['c']:
                self._fitResAction('fit', mat_feature, vec_target, 1.,
                                   capacitance=True)
            # reset fit data
            self.resetFitData()
        else:
             warnings.warn('No fit matrices are stored, no fit has been performed', UserWarning)

    def computeGMCombined(self,
                            dv_mat=None, v_mat=None, i_mat=None,
                            z_mat_arg=None,
                            p_open_channels=None, p_open_other_channels={},
                            e_eqs=None, freqs=0., w_e_eqs=None, w_freqs=None,
                            channel_name=None,
                            weight_fit1=1., weight_fit2=1.):
        '''
        Fit the models' conductances to a given impedance matrix.

        Parameters
        ----------
        z_mat_arg: np.ndarray (ndim = 2 or 3, dtype = float or complex) or
                   list of np.ndarray (ndim = 2 or 3, dtype = float or complex)
            If a single array, represents the steady state impedance matrix,
            If a list of arrays, represents the steady state impedance
            matrices for each equilibrium potential in ``e_eqs``
        e_eqs: np.ndarray (ndim = 1, dtype = float) or float
            The equilibirum potentials in each compartment for each
            evaluation of ``z_mat``
        freqs: ``None`` or `np.array` of `complex
            Frequencies at which the impedance matrices are evaluated. If None,
            assumes that the steady state impedance matrices are provides
        channel_names: ``None`` or `list` of `string`
            The channel types to be included in the fit. If ``None``, all channel
            types that have been added to the tree are included.
        other_channel_names: ``None`` or `list` of `string`
            The channels that are not to be included in the fit
        '''
        # matrices for impedance matrix fit
        channel_names = list(p_open_channels.keys())
        other_channel_names = list(p_open_other_channels.keys()) + ['L']
        mat_feature_1, vec_target_1 = \
                self.computeGM(z_mat_arg,
                               e_eqs=e_eqs, freqs=freqs, w_e_eqs=w_e_eqs, w_freqs=w_freqs,
                               channel_names=channel_names,
                               other_channel_names=other_channel_names,
                               return_matrices=True)
        # matrices for traces fit
        mat_feature_2, vec_target_2 = \
                self.computeGChanFromTrace(dv_mat, v_mat, i_mat,
                                p_open_channels=p_open_channels,
                                p_open_other_channels=p_open_other_channels,
                                action='return')
        # arange matrices for fit
        w1 = weight_fit1 / float(len(vec_target_1))
        w2 = weight_fit2 / float(len(vec_target_2))
        mat_feature = np.concatenate((w1 * mat_feature_1, w2 * mat_feature_2), axis=0)
        vec_target = np.concatenate((w1 * vec_target_1, w2 * vec_target_2))
        # linear regression fit
        res = la.lstsq(mat_feature, vec_target)
        g_vec = res[0].real
        print('g_vec =', g_vec)
        # set the conductances
        self._toTreeGM(g_vec, channel_names=channel_names)

    # def _toVecGChan(self, i_, v_,
    #                 channel_names=None, other_channel_names=None):
    #     self.setEEq(v_)
    #     i_vec = np.zeros(len(self))
    #     d_vec = np.zeros((len(self), len(channel_names)))
    #     for ii, node in enumerate(self):
    #         i_vec[ii] += i_[node.loc_ind]
    #         # add the channel terms
    #         i_vec[ii] -= node.getITot(channel_names=other_channel_names,
    #                                   channel_storage=self.channel_storage)
    #         # add the coupling terms
    #         pnode = node.parent_node
    #         if pnode is not None:
    #             i_vec[ii] += node.g_c * (v_[pnode.loc_ind] - v_[node.loc_ind])
    #         for cnode in node.child_nodes:
    #             i_vec[ii] += cnode.g_c * (v_[cnode.loc_ind] - v_[node.loc_ind])
    #         # drive terms
    #         for kk, channel_name in enumerate(channel_names):
    #             d_vec[ii, kk] = node.getDrive(channel_name,
    #                                           channel_storage=self.channel_storage)

        # return i_vec, d_vec

    def computeFakeGeometry(self, fake_c_m=1., fake_r_a=100.*1e-6,
                                  factor_r_a=1e-6, delta=1e-14,
                                  method=2):
        '''
        Computes a fake geometry so that the neuron model is a reduced
        compurtmental model

        Parameters
        ----------
        fake_c_m: float [uF / cm^2]
            fake membrane capacitance value used to compute the surfaces of
            the compartments
        fake_r_a: float [MOhm * cm]
            fake axial resistivity value, used to evaluate the lengths of each
            section to yield the correct coupling constants

        Returns
        -------
        radii, lengths: np.array of floats [cm]
            The radii, lengths, resp. surfaces for the section in NEURON. Array
            index corresponds to NEURON index

        Raises
        ------
        AssertionError
            If the node indices are not ordered consecutively when iterating
        '''

        assert self.checkOrdered()
        factor_r = 1. / np.sqrt(factor_r_a)
        # compute necessary vectors for calculating
        surfaces = np.array([node.ca / fake_c_m for node in self])
        vec_coupling = np.array([1.] + [1./node.g_c for node in self if \
                                            node.parent_node is not None])
        if method == 1:
            # find the 3d points to construct the segments' geometry
            p0s = -surfaces
            p1s = np.zeros_like(p0s)
            p2s = np.pi * (factor_r**2 - 1.) * np.ones_like(p0s)
            p3s = 2. * np.pi**2 * vec_coupling / fake_r_a * (1. + factor_r)
            # find the polynomial roots
            points = []
            for ii, (p0, p1, p2, p3) in enumerate(zip(p0s, p1s, p2s, p3s)):
                res = np.roots([p3,p2,p1,p0])
                # compute radius and length of first half of section
                radius = res[np.where(res.real > 0.)[0][0]].real
                radius *= 1e4 # convert [cm] to [um]
                length = np.pi * radius**2 * vec_coupling[ii] / (fake_r_a * 1e4) # convert [MOhm*cm] to [MOhm*um]
                # compute the pt3d points
                point0 = [0., 0., 0., 2.*radius]
                point1 = [length, 0., 0., 2.*radius]
                point2 = [length*(1.+delta), 0., 0., 2.*radius*factor_r]
                point3 = [length*(2.+delta), 0., 0., 2.*radius*factor_r]
                points.append([point0, point1, point2, point3])

            return points, surfaces
        elif method == 2:
            radii = np.cbrt(fake_r_a * surfaces / (vec_coupling * (2.*np.pi)**2))
            lengths = surfaces / (2. * np.pi * radii)
            return lengths, radii
        else:
            raise ValueError('Invalid `method` argument, should be 1 or 2')


    def plotDendrogram(self, ax,
                        plotargs={}, labelargs={}, textargs={},
                        nodelabels={},
                        y_max=None):
        '''
        Generate a dendrogram of the NET

        Parameters
        ----------
            ax: :class:`matplotlib.axes`
                the axes object in which the plot will be made
            plotargs : dict (string : value)
                keyword args for the matplotlib plot function, specifies the
                line properties of the dendrogram
            labelargs : dict (string : value)
                keyword args for the matplotlib plot function, specifies the
                marker properties for the node points. Or dict with keys node
                indices, and with values dicts with keyword args for the
                matplotlib function that specify the marker properties for
                specific node points. The entry under key -1 specifies the
                properties for all nodes not explicitly in the keys.
            textargs : dict (string : value)
                keyword args for matplotlib textproperties
            nodelabels: dict (int: string) or None
                labels of the nodes. If None, nodes are named by default
                according to their location indices. If empty dict, no labels
                are added.
            y_max: int, float or None
                specifies the y-scale. If None, the scale is computed from
                ``self``. By default, y=1 is added for each child of a node, so
                if y_max is smaller than the depth of the tree, part of it will
                not be plotted
        '''
        # get the number of leafs to determine the dendrogram spacing
        rnode    = self.root
        n_branch  = self.degreeOfNode(rnode)
        l_spacing = np.linspace(0., 1., n_branch+1)
        if y_max is None:
            y_max = np.max([self.depthOfNode(n) for n in self.leafs]) + 1.5
        y_min = .5
        # plot the dendrogram
        self._expandDendrogram(rnode, 0.5, None, 0.,
                    l_spacing, y_max, ax,
                    plotargs=plotargs, labelargs=labelargs, textargs=textargs,
                    nodelabels=nodelabels)
        # limits
        ax.set_ylim((y_min, y_max))
        ax.set_xlim((0.,1.))

        ax.axes.get_xaxis().set_visible(False)
        ax.axes.get_yaxis().set_visible(False)
        ax.axison = False

        return y_max

    def _expandDendrogram(self, node, x0, xprev, y0,
                                        l_spacing, y_max, ax,
                                        plotargs={}, labelargs={}, textargs={},
                                        nodelabels={}):
        # impedance of layer
        ynew = y0 + 1.
        # plot vertical connection line
        # ax.vlines(x0, y0, ynew, **plotargs)
        if xprev is not None:
            ax.plot([xprev, x0], [y0, ynew], **plotargs)
        # get the child nodes for recursion
        l0 = 0
        for i, cnode in enumerate(node.child_nodes):
            # attribute space on xaxis
            deg = self.degreeOfNode(cnode)
            l1 = l0 + deg
            # new quantities
            xnew = (l_spacing[l0] + l_spacing[l1]) / 2.
            # horizontal connection line limits
            if i == 0:
                xnew0 = xnew
            if i == len(node.child_nodes)-1:
                xnew1 = xnew
            # recursion
            self._expandDendrogram(cnode, xnew, x0, ynew,
                    l_spacing[l0:l1+1], y_max, ax,
                    plotargs=plotargs, labelargs=labelargs, textargs=textargs,
                    nodelabels=nodelabels)
            # next index
            l0 = l1
        # add label and maybe text annotation to node
        if node.index in labelargs:
            ax.plot([x0], [ynew], **labelargs[node.index])
        elif -1 in labelargs:
            ax.plot([x0], [ynew], **labelargs[-1])
        else:
            try:
                ax.plot([x0], [ynew], **labelargs)
            except TypeError as e:
                pass
        if textargs:
            if nodelabels != None:
                if node.index in nodelabels:
                    if labelargs == {}:
                        ax.plot([x0], [ynew], **nodelabels[node.index][1])
                        ax.annotate(nodelabels[node.index][0],
                                    xy=(x0, ynew), xytext=(x0+0.04, ynew+y_max*0.04),
                                    bbox=dict(boxstyle='round', ec=(1., 0.5, 0.5), fc=(1., 0.8, 0.8)),
                                    **textargs)
                    else:
                        ax.annotate(nodelabels[node.index],
                                    xy=(x0, ynew), xytext=(x0+0.04, ynew+y_max*0.04),
                                    bbox=dict(boxstyle='round', ec=(1., 0.5, 0.5), fc=(1., 0.8, 0.8)),
                                    **textargs)
            else:
                ax.annotate(r'$N='+''.join([str(ind) for ind in node.loc_inds])+'$',
                                 xy=(x0, ynew), xytext=(x0+0.04, ynew+y_max*0.04),
                                 bbox=dict(boxstyle='round', ec=(1., 0.5, 0.5), fc=(1., 0.8, 0.8)),
                                 **textargs)










