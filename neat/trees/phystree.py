"""
File contains:

    - :class:`PhysNode`
    - :class:`PhysTree`

Author: W. Wybo
"""

import numpy as np

import warnings

from . import morphtree
from .morphtree import MorphNode, MorphTree
from ..channels import concmechs, ionchannels


def originalTreeModificationDecorator(fun):
    """
    Decorator that provides the safety that the treetype is set to
    'original' inside the functions, and that the computational tree is removed.
    """
    # wrapper to access self
    def wrapped(self, *args, **kwargs):
        self.treetype = 'original'
        res = fun(self, *args, **kwargs)
        self._computational_root = None
        return res
    wrapped.__doc__ = fun.__doc__
    return wrapped


class PhysNode(MorphNode):
    """
    Node associated with `neat.PhysTree`. Stores the physiological parameters
    of the cylindrical segment connecting this node with its parent node

    Attributes
    ----------
    currents: dict {str: [float,float]}
        dict with as keys the channel names and as values lists of length two
        containing as first entry the channels' conductance density (uS/cm^2)
        and as second element the channels reversal (mV) (i.e.:
        {name: [g_max (uS/cm^2), e_rev (mV)]})
        For the leak conductance, the corresponding key is 'L'
    concmechs: dict
        dict containing concentration mechanisms present in the segment
    c_m: float
        The sement's specific membrane capacitance (uF/cm^2)
    r_a: float
        The segment's axial resistance (MOhm*cm)
    g_shunt: float
        Point-like shunt conductance located at x=1 (uS)
    e_eq: float
        Segment's equilibrium potential
    """
    def __init__(self, index, p3d=None,
                       c_m=1., r_a=100*1e-6, g_shunt=0., v_ep=-75.):
        super().__init__(index, p3d)
        self.currents = {} #{name: (g_max (uS/cm^2), e_rev (mV))}
        self.concmechs = {}
        # biophysical parameters
        self.c_m = c_m # uF/cm^2
        self.r_a = r_a # MOhm*cm
        self.g_shunt = g_shunt # uS
        self.v_ep = v_ep # mV
        self.conc_eps = {} # equilibrium concentration values (mM)

    def setPhysiology(self, c_m, r_a, g_shunt=0.):
        """
        Set the physiological parameters of the current

        Parameters
        ----------
        c_m: float
            the membrance capacitance (uF/cm^2)
        r_a: float
            the axial current (MOhm*cm)
        g_shunt: float
            A point-like shunt, located at x=1 on the node. Defaults to 0.
        """
        self.c_m = c_m # uF/cm^2
        self.r_a = r_a # MOhm*cm
        self.g_shunt = g_shunt

    def _addCurrent(self, channel_name, g_max, e_rev):
        """
        Add an ion channel current at this node. ('L' as `channel_name`
        signifies the leak current)

        Parameters
        ----------
        channel_name: string
            the name of the current
        g_max: float
            the conductance of the current (uS/cm^2)
        e_rev: float
            the reversal potential of the current (mV)
        """
        self.currents[channel_name] = [g_max, e_rev]

    def addConcMech(self, ion, params={}):
        """
        Add a concentration mechanism at this node.

        Parameters
        ----------
        ion: string
            the ion the mechanism is for
        params: dict
            parameters for the concentration mechanism (only used for NEURON model)
        """
        if set(params.keys()) == {'gamma', 'tau'}:
            self.concmechs[ion] = concmechs.ExpConcMech(ion,
                                        params['tau'], params['gamma'])
        else:
            warnings.warn('These parameters do not match any NEAT concentration ' + \
                          'mechanism, no concentration mechanism has been added', UserWarning)

    def setVEP(self, v_ep):
        """
        Set the equilibrium potential at the node.

        Parameters
        ----------
        e_eq: float
            the equilibrium potential (mV)
        """
        self.v_ep = v_ep

    def setConcEP(self, ion, conc):
        """
        Set the equilibrium concentration value at this node

        Parameters
        ----------
        ion: str ('ca', 'k', 'na')
            the ion for which the concentration is to be set
        conc: float
            the concentration value (mM)
        """
        self.conc_eps[ion] = conc

    def fitLeakCurrent(self, channel_storage, e_eq_target=-75., tau_m_target=10.):
        """
        """
        gsum = 0.
        i_eq = 0.

        for channel_name in set(self.currents.keys()) - set('L'):
            g, e = self.currents[channel_name]

            # compute channel conductance and current
            p_open = channel_storage[channel_name].computePOpen(e_eq_target)
            g_chan = g * p_open

            gsum += g_chan
            i_eq += g_chan * (e - e_eq_target)

        if self.c_m / (tau_m_target*1e-3) < gsum:
            warnings.warn('Membrane time scale is chosen larger than ' + \
                          'possible, adding small leak conductance')
            tau_m_target = self.c_m / (gsum + 20.)
        else:
            tau_m_target *= 1e-3
        g_l = self.c_m / tau_m_target - gsum
        e_l = e_eq_target - i_eq / g_l
        self.currents['L'] = [g_l, e_l]
        self.v_ep = e_eq_target

    def getGTot(self, channel_storage, channel_names=None, v=None):
        """
        Get the total conductance of the membrane at a steady state given voltage,
        if nothing is given, the equilibrium potential is used to compute membrane
        conductance.

        Parameters
        ----------
            channel_names: List[str]
                the names of the channels to be included included in the
                conductance calculation
            channel_storage: dict {``channel_name``: `channel_instance`}
                dict where all ion channel objects present on the node are stored
            v: float (optional, defaults to `self.v_ep`)
                the potential (in mV) at which to compute the membrane conductance

        Returns
        -------
            float
                the total conductance of the membrane (uS / cm^2)
        """
        if channel_names is None:
            channel_names = channel_names = list(self.currents.keys())
        v = self.v_ep if v is None else v

        g_tot = 0.
        for channel_name in set(self.currents.keys()) & set(channel_names):
            g, e = self.currents[channel_name]

            if channel_name == 'L':
                g_tot += g
            else:
                g_tot += g * channel_storage[channel_name].computePOpen(v)

        return g_tot

    def getITot(self, channel_storage, channel_names=None, v=None):
        """
        Get the total conductance of the membrane at a steady state given voltage,
        if nothing is given, the equilibrium potential is used to compute membrane
        conductance.

        Parameters
        ----------
            channel_names: List[str]
                the names of the channels to be included included in the
                conductance calculation
            channel_storage: dict {``channel_name``: `channel_instance`}
                dict where all ion channel objects present on the node are stored
            v: float (optional, defaults to `self.v_ep`)
                the potential (in mV) at which to compute the membrane conductance

        Returns
        -------
            float
                the total conductance of the membrane (uS / cm^2)
        """
        if channel_names is None:
            channel_names = channel_names = list(self.currents.keys())
        v = self.v_ep if v is None else v

        i_tot = 0.
        for channel_name in set(self.currents.keys()) & set(channel_names):
            g, e = self.currents[channel_name]

            if channel_name == 'L':
                i_tot += g * (v - e)
            else:
                p_open = channel_storage[channel_name].computePOpen(v)
                i_tot += g * p_open * (v - e)

        return i_tot

    def asPassiveMembrane(self, channel_storage, channel_names=None, v=None):
        if channel_names is None:
            channel_names = list(self.currents.keys())
        # append leak current to channel names
        if "L" not in channel_names:
            channel_names.append("L")

        v = self.v_ep if v is None else v

        # compute the total conductance of the to be passified channels
        g_l = self.getGTot(channel_storage, channel_names=channel_names, v=v)

        # compute the total current of the not to be passified channels
        i_tot = self.getITot(channel_storage,
            channel_names=[
                key for key in channel_storage if key not in channel_names
            ],
            v=v,
        )

        # remove the passified channels
        for channel_name in channel_names:
            if channel_name == 'L':
                continue

            try:
                del self.currents[channel_name]
            except KeyError:
                # the channel was not present at this node anyway
                pass

        self.currents['L'] = [g_l, v + i_tot / g_l]

    def __str__(self, with_parent=True, with_morph_info=False):
        if with_morph_info:
            node_str = super().__str__(with_parent=with_parent)
        else:
            node_str = super(MorphNode, self).__str__(with_parent=with_parent)

        node_str += f" --- " \
            f"r_a = {self.r_a} MOhm*cm, " \
            f"c_m = {self.c_m} uF/cm^2, " \
            f"v_ep = {self.v_ep} mV, "
        if self.g_shunt > 1e-10:
            f"g_shunt = {self.g_shunt} uS,"
        node_str += ', '.join([
            f'(g_{c} = {g} uS/cm^2, e_{c} = {e} mV)' for c, (g, e) in self.currents.items()
        ])
        return node_str

    def _getReprDict(self):
        repr_dict = super()._getReprDict()
        repr_dict.update({
            "currents": {c: (f"({g:1.6g}, {e:1.6g})") for c, (g, e) in self.currents.items()},
            "concmechs": self.concmechs,
            "c_m": f"{self.c_m:1.6g}",
            "r_a": f"{self.r_a:1.6g}",
            "g_shunt": f"{self.g_shunt:1.6g}",
            "v_ep": f"{self.v_ep:1.6g}",
            "conc_eps": {ion: f"{conc:1.6g}" for ion, conc in self.conc_eps.items()},
        })
        return repr_dict

    def __repr__(self):
        return repr(self._getReprDict())


class PhysTree(MorphTree):
    """
    Adds physiological parameters to `neat.MorphTree` and convenience functions
    to set them across the morphology. Initialized in the same way as
    `neat.MorphTree`

    Functions for setting ion channels densities are applied to the original tree,
    which can cause the computational tree to be out of sync. To avoid this, the
    computational tree is always removed by these functions. It can be set
    afterwards with `PhysTree.setCompTree()`

    Attributes
    ----------
    channel_storage: dict {str: `neat.IonChannel`}
        Stores the user defined ion channels present in the tree
    """
    def __init__(self, file_n=None, types=[1,3,4]):
        super().__init__(file_n=file_n, types=types)
        # set basic physiology parameters (c_m = 1.0 uF/cm^2 and
        # r_a = 0.0001 MOhm*cm)
        for node in self:
            node.setPhysiology(1.0, 100./1e6)
        self.channel_storage = {}
        self.ions = set()

    def _getReprDict(self):
        ckeys = list(self.channel_storage.keys())
        ckeys.sort()
        return {"channel_storage": ckeys}

    def __repr__(self):
        repr_str = super().__repr__()
        return repr_str + repr(self._getReprDict())

    def _resetChannelStorage(self):
        new_channel_storage = {}
        for node in self:
            for channel_name in node.currents:
                if channel_name not in new_channel_storage and \
                   channel_name != "L":
                    new_channel_storage[channel_name] = self.channel_storage[channel_name]

        self.channel_storage = new_channel_storage

    def _createCorrespondingNode(self, node_index, p3d=None,
                                      c_m=1., r_a=100*1e-6, g_shunt=0., v_ep=-75.):
        """
        Creates a node with the given index corresponding to the tree class.

        Parameters
        ----------
            node_index: int
                index of the new node
        """
        return PhysNode(node_index, p3d=p3d)

    @originalTreeModificationDecorator
    def asPassiveMembrane(self, channel_names=None, node_arg=None):
        """
        Makes the membrane act as a passive membrane (for the nodes in
        ``node_arg``), channels are assumed to add a conductance of
        g_max * p_open to the membrane conductance, where p_open for each node
        is evaluated at the expansion point potential stored in that node,
        i.e. `PhysNode.v_ep` (see `PhysTree.setVEP()`).

        Parameters
        ----------
        channel_names: List[str] or None
            The channels to passify. If not provided, all channels are passified.
        node_arg: optional
            see documentation of :func:`MorphTree._convertNodeArgToNodes`.
            Defaults to None. The nodes for which the membrane is set to
            passive
        """
        for node in self._convertNodeArgToNodes(node_arg):
            node.asPassiveMembrane(
                self.channel_storage, channel_names=channel_names
            )

        self._resetChannelStorage()

    def _distr2Float(self, distr, node, argname=''):
        if isinstance(distr, float):
            val = distr
        elif isinstance(distr, dict):
            val = distr[node.index]
        elif hasattr(distr, '__call__'):
            d2s = self.pathLength({'node': node.index, 'x': .5}, (1., 0.5))
            val = distr(d2s)
        else:
            raise TypeError(argname + ' argument should be a float, dict ' + \
                            'or a callable')
        return val

    @originalTreeModificationDecorator
    def setVEP(self, v_ep_distr, node_arg=None):
        """
        Set the voltage expansion points throughout the tree.

        Note that these need not correspond to the actual equilibrium potentials
        in the absence of input, but rather the (node-specific) voltage around
        which the possible expansions are computed.

        Parameters
        ----------
        v_ep_distr: float, dict or :func:`float -> float`
            The expansion point potentials [mV]
        """
        for node in self._convertNodeArgToNodes(node_arg):
            e = self._distr2Float(v_ep_distr, node, argname='`v_ep_distr`')
            node.setVEP(e)

    @originalTreeModificationDecorator
    def setConcEP(self, ion, conc_eq_distr, node_arg=None):
        """
        Set the concentration expansion points throughout the tree.

        Note that these need not correspond to the actual equilibrium concentrations
        in the absence of input, but rather the (node-specific) concentrations around
        which the possible expansions are computed.

        Parameters
        ----------
        conc_eq_distr: float, dict or :func:`float -> float`
            The expansion point concentrations [mM]
        """
        for node in self._convertNodeArgToNodes(node_arg):
            conc = self._distr2Float(conc_eq_distr, node, argname='`conc_eq_distr`')
            node.setConcEP(ion, conc)

    @originalTreeModificationDecorator
    def setPhysiology(self, c_m_distr, r_a_distr, g_s_distr=None, node_arg=None):
        """
        Set specifice membrane capacitance, axial resistance and (optionally)
        static point-like shunt conductances in the tree. Capacitance is stored
        at each node as the attribute 'c_m' (uF/cm2) and axial resistance as the
        attribute 'r_a' (MOhm*cm)

        Parameters
        ----------
        c_m_distr: float, dict or :func:`float -> float`
            specific membrance capacitance
        r_a_distr: float, dict or :func:`float -> float`
            axial resistance
        g_s_distr: float, dict, :func:`float -> float` or None (optional, default
            is `None`)
            point like shunt conductances (placed at `(node.index, 1.)` for the
            nodes in ``node_arg``). By default no shunt conductances are added
        node_arg: optional
            see documentation of :func:`MorphTree._convertNodeArgToNodes`.
            Defaults to None
        """
        for node in self._convertNodeArgToNodes(node_arg):
            c_m = self._distr2Float(c_m_distr, node, argname='`c_m_distr`')
            r_a = self._distr2Float(r_a_distr, node, argname='`r_a_distr`')
            g_s = self._distr2Float(g_s_distr, node, argname='`g_s_distr`') if \
                  g_s_distr is not None else 0.
            node.setPhysiology(c_m, r_a, g_s)

    @originalTreeModificationDecorator
    def setLeakCurrent(self, g_l_distr, e_l_distr, node_arg=None):
        """
        Set the parameters of the leak current. At each node, leak is stored
        under the attribute `node.currents['L']` at a tuple `(g_l, e_l)` with
        `g_l` the conductance [uS/cm^2] and `e_l` the reversal [mV]

        parameters:
        ----------
        g_l_distr: float, dict or :func:`float -> float`
            If float, the leak conductance is set to this value for all
            the nodes specified in `node_arg`. If it is a function, the input
            must specify the distance from the soma (micron) and the output
            the leak conductance [uS/cm^2] at that distance. If it is a
            dict, keys are the node indices and values the ion leak
            conductances [uS/cm^2].
        e_l_distr: float, dict or :func:`float -> float`
            If float, the reversal [mV] is set to this value for all
            the nodes specified in `node_arg`. If it is a function, the input
            must specify the distance from the soma [um] and the output
            the reversal at that distance. If it is a
            dict, keys are the node indices and values the ion reversals.
        node_arg: optional
            see documentation of :func:`MorphTree._convertNodeArgToNodes`.
            Defaults to None
        """
        for node in self._convertNodeArgToNodes(node_arg):
            g_l = self._distr2Float(g_l_distr, node, argname='`g_l_distr`')
            e_l = self._distr2Float(e_l_distr, node, argname='`e_l_distr`')
            node._addCurrent('L', g_l, e_l)

    @originalTreeModificationDecorator
    def addCurrent(self, channel, g_max_distr, e_rev_distr, node_arg=None):
        """
        Adds a channel to the morphology. At each node, the channel is stored
        under the attribute `node.currents[channel.__class__.__name__]` as a
        tuple `(g_max, e_rev)` with `g_max` the maximal conductance [uS/cm^2]
        and `e_rev` the reversal [mV]

        Parameters
        ----------
        channel_name: :class:`IonChannel`
            The ion channel
        g_max_distr: float, dict or :func:`float -> float`
            If float, the maximal conductance is set to this value for all
            the nodes specified in `node_arg`. If it is a function, the input
            must specify the distance from the soma (micron) and the output
            the ion channel density (uS/cm^2) at that distance. If it is a
            dict, keys are the node indices and values the ion channel
            densities (uS/cm^2).
        e_rev_distr: float, dict or :func:`float -> float`
            If float, the reversal (mV) is set to this value for all
            the nodes specified in `node_arg`. If it is a function, the input
            must specify the distance from the soma (micron) and the output
            the reversal at that distance. If it is a
            dict, keys are the node indices and values the ion reversals.
        node_arg: optional
            see documentation of :func:`MorphTree._convertNodeArgToNodes`.
            Defaults to None
        """
        if not isinstance(channel, ionchannels.IonChannel):
            raise IOError('`channel` argmument needs to be of class `neat.IonChannel`')

        channel_name = channel.__class__.__name__
        self.channel_storage[channel_name] = channel
        # add the ion channel to the nodes
        for node in self._convertNodeArgToNodes(node_arg):
            g_max = self._distr2Float(g_max_distr, node, argname='`g_max_distr`')
            e_rev = self._distr2Float(e_rev_distr, node, argname='`e_rev_distr`')
            node._addCurrent(channel_name, g_max, e_rev)

    @morphtree.originalTreetypeDecorator
    def getChannelsInTree(self):
        """
        Returns list of strings of all channel names in the tree

        Returns
        -------
        list of string
            the channel names
        """
        return list(self.channel_storage.keys())

    @originalTreeModificationDecorator
    def addConcMech(self, ion, params={}, node_arg=None):
        """
        Add a concentration mechanism to the tree

        Parameters
        ----------
        ion: string
            the ion the mechanism is for
        params: dict
            parameters for the concentration mechanism (only used for NEURON model)
        node_arg:
            see documentation of :func:`MorphTree._convertNodeArgToNodes`.
            Defaults to None
        """
        self.ions.add(ion)
        for node in self._convertNodeArgToNodes(node_arg):
            node.addConcMech(ion, params=params)

    @originalTreeModificationDecorator
    def fitLeakCurrent(self, e_eq_target_distr, tau_m_target_distr, node_arg=None):
        """
        Fits the leak current to fix equilibrium potential and membrane time-
        scale.

        !!! Should only be called after all ion channels have been added !!!

        Parameters
        ----------
        e_eq_target_distr: float, dict or :func:`float -> float`
            The target reversal potential (mV). If float, the target reversal is
            set to this value for all the nodes specified in `node_arg`. If it
            is a function, the input must specify the distance from the soma (um)
            and the output the target reversal at that distance. If it is a
            dict, keys are the node indices and values the target reversals
        tau_m_target_distr: float, dict or :func:`float -> float`
            The target membrane time-scale (ms). If float, the target time-scale is
            set to this value for all the nodes specified in `node_arg`. If it
            is a function, the input must specify the distance from the soma (um)
            and the output the target time-scale at that distance. If it is a
            dict, keys are the node indices and values the target time-scales
        node_arg:
            see documentation of :func:`MorphTree._convertNodeArgToNodes`.
            Defaults to None
        """
        for node in self._convertNodeArgToNodes(node_arg):
            e_eq_target = self._distr2Float(e_eq_target_distr, node, argname='`g_max_distr`')
            tau_m_target = self._distr2Float(tau_m_target_distr, node, argname='`e_rev_distr`')
            assert tau_m_target > 0.
            node.fitLeakCurrent(e_eq_target=e_eq_target, tau_m_target=tau_m_target,
                                channel_storage=self.channel_storage)

    def _evaluateCompCriteria(self, node, eps=1e-8, rbool=False):
        """
        Return ``True`` if relative difference in any physiological parameters
        between node and child node is larger than margin ``eps``.

        Overrides the `MorphTree._evaluateCompCriteria()` function called by
        `MorphTree.setCompTree()`.

        Parameters
        ----------
        node: ::class::`MorphNode`
            node that is compared to parent node
        eps: float (optional, default ``1e-8``)
            the margin

        return
        ------
        bool
        """
        rbool = super()._evaluateCompCriteria(node, eps=eps, rbool=rbool)

        if not rbool:
            cnode = node.child_nodes[0]
            rbool = np.abs(node.r_a - cnode.r_a) > eps * np.max([node.r_a, cnode.r_a])
        if not rbool:
            rbool = np.abs(node.c_m - cnode.c_m) > eps * np.max([node.c_m, cnode.c_m])
        if not rbool:
            rbool = set(node.currents.keys()) != set(cnode.currents.keys())
        if not rbool:
            for chan_name, channel in node.currents.items():
                if not rbool:
                    rbool = np.abs(channel[0] - cnode.currents[chan_name][0]) > eps * \
                             np.max([np.abs(channel[0]), np.abs(cnode.currents[chan_name][0])])
                if not rbool:
                    rbool = np.abs(channel[1] - cnode.currents[chan_name][1]) > eps * \
                             np.max([np.abs(channel[1]), np.abs(cnode.currents[chan_name][1])])
        if not rbool:
            rbool = node.g_shunt > 0.001*eps

        return rbool

    # @morphtree.originalTreetypeDecorator
    # def _calcFdMatrix(self, dx=10.):
    #     matdict = {}
    #     locs = [{'node': 1, 'x': 0.}]
    #     # set the first element
    #     soma = self.tree.root
    #     matdict[(0,0)] = 4.0*np.pi*soma.R**2 * soma.G
    #     # recursion
    #     cnodes = root.getChildNodes()[2:]
    #     numel_l = [1]
    #     for cnode in cnodes:
    #         if not is_changenode(cnode):
    #             cnode = find_previous_changenode(cnode)[0]
    #         self._fdMatrixFromRoot(cnode, root, 0, numel_l, locs, matdict, dx=dx)
    #     # create the matrix
    #     FDmat = np.zeros((len(locs), len(locs)))
    #     for ind in matdict:
    #         FDmat[ind] = matdict[ind]

    #     return FDmat, locs # caution, not the reduced locs yet

    # def _fdMatrixFromRoot(self, node, pnode, ibranch, numel_l, locs, matdict, dx=10.*1e-4):
    #     numel = numel_l[0]
    #     # distance between the two nodes and radius of the cylinder
    #     radius *= node.R*1e-4; length *= node.L*1e-4
    #     num = np.around(length/dx)
    #     xvals = np.linspace(0.,1.,max(num+1,2))
    #     dx_ = xvals[1]*length
    #     # set the first element
    #     matdict[(ibranch,numel)] = - np.pi*radius**2 / (node.r_a*dx_)
    #     matdict[(ibranch,ibranch)] += np.pi*radius**2 / (node.r_a*dx_)
    #     matdict[(numel,numel)] = 2.*np.pi*radius**2 / (node.r_a*dx_)
    #     matdict[(numel,ibranch)] = - np.pi*radius**2 / (node.r_a*dx_)
    #     locs.append({'node': node._index, 'x': xvals[1]})
    #     # set the other elements
    #     if len(xvals) > 2:
    #         i = 0; j = 0
    #         if len(xvals) > 3:
    #             for x in xvals[2:-1]:
    #                 j = i+1
    #                 matdict[(numel+i,numel+j)] = - np.pi*radius**2 / (node.r_a*dx_)
    #                 matdict[(numel+j,numel+j)] = 2. * np.pi*radius**2 / (node.r_a*dx_)
    #                                            # + 2.*np.pi*radius*dx_*node.G
    #                 matdict[(numel+j,numel+i)] = - np.pi*radius**2 / (node.r_a*dx_)
    #                 locs.append({'node': node._index, 'x': x})
    #                 i += 1
    #         # set the last element
    #         j = i+1
    #         matdict[(numel+i,numel+j)] = - np.pi*radius**2 / (node.r_a*dx_)
    #         matdict[(numel+j,numel+j)] = np.pi*radius**2 / (node.r_a*dx_)
    #         matdict[(numel+j,numel+i)] = - np.pi*radius**2 / (node.r_a*dx_)
    #         locs.append({'node': node._index, 'x': 1.})
    #     numel_l[0] = numel+len(xvals)-1
    #     # assert numel_l[0] == len(locs)
    #     # if node is leaf, then implement other bc
    #     if len(xvals) > 2:
    #         ibranch = numel+j
    #     else:
    #         ibranch = numel
    #     # move on the further elements
    #     for cnode in node.child_nodes:
    #         self._fdMatrixFromRoot(cnode, node, ibranch, numel_l, locs, matdict, dx=dx)






