# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2016 - Bernd Hahnebach <bernd@bimstatik.org>            *
# *   Copyright (c) 2017 Johan Heyns (CSIR) <jheyns@csir.co.za>             *
# *   Copyright (c) 2017-2018 Oliver Oxtoby (CSIR) <ooxtoby@csir.co.za>     *
# *   Copyright (c) 2017 Alfred Bogaers (CSIR) <abogaers@csir.co.za>        *
# *   Copyright (c) 2019 Oliver Oxtoby <oliveroxtoby@gmail.com>             *
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU Lesser General Public License (LGPL)    *
# *   as published by the Free Software Foundation; either version 2 of     *
# *   the License, or (at your option) any later version.                   *
# *   for detail see the LICENCE text file.                                 *
# *                                                                         *
# *   This program is distributed in the hope that it will be useful,       *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
# *   GNU Library General Public License for more details.                  *
# *                                                                         *
# *   You should have received a copy of the GNU Library General Public     *
# *   License along with this program; if not, write to the Free Software   *
# *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  *
# *   USA                                                                   *
# *                                                                         *
# ***************************************************************************

__title__ = "Tools for mesh generation using snappyHexMesh, cfMesh and gmsh"
__author__ = "AB, JH, OO, Bernd Hahnebach"
__url__ = "http://www.freecadweb.org"

import FreeCAD
import Fem
try:
    import femmesh.meshtools as FemMeshTools
except ImportError:  # Backward compatibility
    import FemMeshTools
try:
    from femobjects import _FemMeshGmsh
except ImportError:  # Backward compat
    from PyObjects import _FemMeshGmsh
from FreeCAD import Units
import os
import platform
import shutil
import subprocess
import CfdTools
import math
import MeshPart
import TemplateBuilder
import Part
import CfdCaseWriterFoam


class CfdMeshTools:
    def __init__(self, cart_mesh_obj):
        self.mesh_obj = cart_mesh_obj
        self.analysis = CfdTools.getParentAnalysisObject(self.mesh_obj)

        self.part_obj = self.mesh_obj.Part  # Part to mesh
        self.scale = 0.001  # Scale mm to m

        # Default to 2 % of bounding box characteristic length
        self.clmax = Units.Quantity(self.mesh_obj.CharacteristicLengthMax).Value
        if self.clmax <= 0.0:
            shape = self.part_obj.Shape
            cl_bound_box = math.sqrt(shape.BoundBox.XLength**2 + shape.BoundBox.YLength**2 + shape.BoundBox.ZLength**2)
            self.clmax = 0.02*cl_bound_box  # Always in internal format, i.e. mm

        # Only used by gmsh - what purpose?
        self.clmin = 0.0

        self.dimension = self.mesh_obj.ElementDimension

        shape_face_names = []
        for (i, f) in enumerate(self.part_obj.Shape.Faces):
            face_name = ("face{}".format(i))
            shape_face_names.append(face_name)
        self.mesh_obj.ShapeFaceNames = shape_face_names

        self.cf_settings = {}
        self.snappy_settings = {}
        self.gmsh_settings = {}
        self.two_d_settings = {}

        self.error = False

        output_path = CfdTools.getOutputPath(self.analysis)
        self.getFilePaths(output_path)

    def processDimension(self):
        """ Additional checking/processing for 2D vs 3D """
        # 3D cfMesh and snappyHexMesh, and 2D by conversion, while in future cfMesh may support 2D directly
        if self.dimension != '3D' and self.dimension != '2D':
            FreeCAD.Console.PrintError('Invalid element dimension. Setting to 3D.')
            self.dimension = '3D'
        print('  ElementDimension: ' + self.dimension)

        # Check for 2D boundaries
        twoDPlanes = []
        analysis_obj = CfdTools.getParentAnalysisObject(self.mesh_obj)
        if not analysis_obj:
            analysis_obj = CfdTools.getActiveAnalysis()
        if analysis_obj:
            boundaries = CfdTools.getCfdBoundaryGroup(analysis_obj)
            for b in boundaries:
                if b.BoundaryType == 'constraint' and \
                   b.BoundarySubType == 'twoDBoundingPlane':
                    twoDPlanes.append(b.Name)

        if self.dimension == '2D':
            self.two_d_settings['ConvertTo2D'] = True
            if len(twoDPlanes) != 2:
                raise RuntimeError("For 2D meshing, two separate, parallel, 2D bounding planes must be present as "
                                   "boundary conditions in the CFD analysis object.")
            doc_name = str(analysis_obj.Document.Name)
            fFObjName = twoDPlanes[0]
            bFObjName = twoDPlanes[1]
            frontObj = FreeCAD.getDocument(doc_name).getObject(fFObjName)
            backObj = FreeCAD.getDocument(doc_name).getObject(bFObjName)
            fShape = frontObj.Shape
            bShape = backObj.Shape
            if len(fShape.Faces) == 0 or len(bShape.Faces) == 0:
                raise RuntimeError("A 2D bounding plane is empty.")
            else:
                allFFacesPlanar = True
                allBFacesPlanar = True
                for faces in fShape.Faces:
                    if not isinstance(faces.Surface, Part.Plane):
                        allFFacesPlanar = False
                        break
                for faces in bShape.Faces:
                    if not isinstance(faces.Surface, Part.Plane):
                        allBFacesPlanar = False
                        break
                if allFFacesPlanar and allBFacesPlanar:
                    A1 = fShape.Faces[0].Surface.Axis
                    A1.multiply(1.0/A1.Length)
                    A2 = bShape.Faces[0].Surface.Axis
                    A2.multiply(1.0/A2.Length)
                    if (A1-A2).Length <= 1e-6 or (A1+A2).Length <= 1e-6:
                        if len(frontObj.Shape.Vertexes) == len(backObj.Shape.Vertexes) and \
                           len(frontObj.Shape.Vertexes) > 0 and \
                           abs(frontObj.Shape.Area) > 0 and \
                           abs(frontObj.Shape.Area - backObj.Shape.Area)/abs(frontObj.Shape.Area) < 1e-6:
                            self.two_d_settings['Distance'] = fShape.distToShape(bShape)[0]/1000
                        else:
                            raise RuntimeError("2D bounding planes do not match up.")
                    else:
                        raise RuntimeError("2D bounding planes are not aligned.")
                else:
                    raise RuntimeError("2D bounding planes need to be flat surfaces.")

            case = CfdCaseWriterFoam.CfdCaseWriterFoam(analysis_obj)
            case.settings = {}
            case.settings['createPatchesFromSnappyBaffles'] = False
            case.setupPatchNames()
            keys = list(case.settings['createPatches'].keys())

            frontPatchIndex = keys.index(frontObj.Label)
            self.two_d_settings['FrontFaceList'] = case.settings['createPatches'][keys[frontPatchIndex]]['PatchNamesList']

            backPatchIndex = keys.index(backObj.Label)
            self.two_d_settings['BackFaceList'] = case.settings['createPatches'][keys[backPatchIndex]]['PatchNamesList']
            self.two_d_settings['BackFace'] = self.two_d_settings['BackFaceList'][0]
        else:
            self.two_d_settings['ConvertTo2D'] = False
            if len(twoDPlanes):
                raise RuntimeError("2D bounding planes can not be used in 3D mesh")

    def getClmax(self):
        return Units.Quantity(self.clmax, Units.Length)

    def getFilePaths(self, output_dir):
        if not hasattr(self.mesh_obj, 'CaseName'):  # Backward compat
            self.mesh_obj.CaseName = 'meshCase'
        self.case_name = self.mesh_obj.CaseName
        self.meshCaseDir = os.path.join(output_dir, self.case_name)
        self.constantDir = os.path.join(self.meshCaseDir, 'constant')
        self.polyMeshDir = os.path.join(self.constantDir, 'polyMesh')
        self.triSurfaceDir = os.path.join(self.constantDir, 'triSurface')
        self.gmshDir = os.path.join(self.meshCaseDir, 'gmsh')
        self.systemDir = os.path.join(self.meshCaseDir, 'system')

        if self.mesh_obj.MeshUtility == "gmsh":
            self.temp_file_shape = os.path.join(self.gmshDir, self.part_obj.Name +"_Geometry.brep")
            self.temp_file_geo = os.path.join(self.gmshDir, self.part_obj.Name +"_Geometry.geo")
            self.temp_file_mesh = os.path.join(self.gmshDir, self.part_obj.Name + '_Geometry.msh')
        else:
            self.temp_file_geo = os.path.join(self.constantDir, 'triSurface', self.part_obj.Name + '_Geometry.stl')

    def setupMeshCaseDir(self):
        """ Create temporary mesh case directory """
        if os.path.isdir(self.meshCaseDir):
            shutil.rmtree(self.meshCaseDir)
        os.makedirs(self.meshCaseDir)
        os.makedirs(self.constantDir)
        os.makedirs(self.triSurfaceDir)
        os.makedirs(self.gmshDir)
        os.makedirs(self.systemDir)

    def processRefinements(self):
        """ Process mesh refinements """
        mr_objs = CfdTools.getMeshRefinementObjs(self.mesh_obj)

        if self.mesh_obj.MeshUtility == "gmsh":
            # mesh regions
            self.ele_length_map = {}  # { 'ElementString' : element length }
            self.ele_node_map = {}  # { 'ElementString' : [element nodes] }
            if not mr_objs:
                print ('  No mesh refinements')
            else:
                print ('  Mesh refinements found - getting elements')
                if self.part_obj.Shape.ShapeType == 'Compound':
                    # see http://forum.freecadweb.org/viewtopic.php?f=18&t=18780&start=40#p149467 and http://forum.freecadweb.org/viewtopic.php?f=18&t=18780&p=149520#p149520
                    err = "GMSH could return unexpected meshes for a boolean split tools Compound. It is strongly recommended to extract the shape to mesh from the Compound and use this one."
                    FreeCAD.Console.PrintError(err + "\n")
                for mr_obj in mr_objs:
                    if mr_obj.RelativeLength:
                        if mr_obj.References:
                            for sub in mr_obj.References:
                                # Check if the shape of the mesh region is an element of the Part to mesh;
                                # if not try to find the element in the shape to mesh
                                search_ele_in_shape_to_mesh = False
                                ref = FreeCAD.ActiveDocument.getObject(sub[0])
                                if not self.part_obj.Shape.isSame(ref.Shape):
                                    search_ele_in_shape_to_mesh = True
                                elems = sub[1]
                                if search_ele_in_shape_to_mesh:
                                    # Try to find the element in the Shape to mesh
                                    ele_shape = FemMeshTools.get_element(ref, elems)  # the method getElement(element) does not return Solid elements
                                    found_element = CfdTools.findElementInShape(self.part_obj.Shape, ele_shape)
                                    if found_element:
                                        elems = found_element
                                    else:
                                        FreeCAD.Console.PrintError("One element of the meshregion " + mr_obj.Name + " could not be found in the Part to mesh. It will be ignored.\n")
                                        elems = None
                                if elems:
                                    if elems not in self.ele_length_map:
                                        # self.ele_length_map[elems] = Units.Quantity(mr_obj.CharacteristicLength).Value
                                        mr_rellen = mr_obj.RelativeLength
                                        if mr_rellen > 1.0:
                                            mr_rellen = 1.0
                                            FreeCAD.Console.PrintError("The meshregion: " + mr_obj.Name + " should not use a relative length greater than unity.\n")
                                        elif mr_rellen < 0.01:
                                            mr_rellen = 0.01  # Relative length should not be less than 1/100 of base length
                                            FreeCAD.Console.PrintError("The meshregion: " + mr_obj.Name + " should not use a relative length smaller than 0.01.\n")
                                        self.ele_length_map[elems] = mr_rellen*self.clmax
                                    else:
                                        FreeCAD.Console.PrintError("The element " + elems + " of the mesh refinement " + mr_obj.Name + " has been added to another mesh refinement.\n")
                        else:
                            FreeCAD.Console.PrintError("The meshregion: " + mr_obj.Name + " is not used to create the mesh because the reference list is empty.\n")
                    else:
                        FreeCAD.Console.PrintError("The meshregion: " + mr_obj.Name + " is not used to create the mesh because the CharacteristicLength is 0.0 mm.\n")
                for eleml in self.ele_length_map:
                    ele_shape = FemMeshTools.get_element(self.part_obj, eleml)  # the method getElement(element) does not return Solid elements
                    ele_vertexes = FemMeshTools.get_vertexes_by_element(self.part_obj.Shape, ele_shape)
                    self.ele_node_map[eleml] = ele_vertexes

        else:
            cf_settings = self.cf_settings
            cf_settings['MeshRegions'] = {}
            cf_settings['BoundaryLayers'] = {}
            cf_settings['InternalRegions'] = {}
            snappy_settings = self.snappy_settings
            snappy_settings['MeshRegions'] = {}
            snappy_settings['InternalRegions'] = {}

            from collections import defaultdict
            ele_meshpatch_map = defaultdict(list)
            if not mr_objs:
                print('  No mesh refinement')
            else:
                print('  Mesh refinements - getting the elements')
                if "Boolean" in self.part_obj.Name:
                    err = "Cartesian meshes should not be generated for boolean split compounds."
                    FreeCAD.Console.PrintError(err + "\n")

                # Make list of list of all references for their corresponding mesh object
                bl_matched_faces = []
                if self.mesh_obj.MeshUtility == 'cfMesh':
                    region_face_lists = []
                    for mr_id, mr_obj in enumerate(mr_objs):
                        region_face_lists.append([])
                        if mr_obj.NumberLayers > 1 and not mr_obj.Internal:
                            refs = mr_obj.References
                            for r in refs:
                                region_face_lists[mr_id].append(r)
                    bl_matched_faces = CfdTools.matchFacesToTargetShape(region_face_lists, self.mesh_obj.Part.Shape)

                for mr_id, mr_obj in enumerate(mr_objs):
                    Internal = mr_obj.Internal

                    if mr_obj.RelativeLength:
                        # Store parameters per region
                        mr_rellen = mr_obj.RelativeLength
                        if mr_rellen > 1.0:
                            mr_rellen = 1.0
                            FreeCAD.Console.PrintError(
                                "The meshregion: {} should not use a relative length greater "
                                "than unity.\n".format(mr_obj.Name))
                        elif mr_rellen < 0.001:
                            mr_rellen = 0.001  # Relative length should not be less than 0.1% of base length
                            FreeCAD.Console.PrintError(
                                "The meshregion: {} should not use a relative length smaller "
                                "than 0.001.\n".format(mr_obj.Name))

                        tri_surface = ""
                        snappy_mesh_region_list = []
                        patch_list = []
                        for (si, sub) in enumerate(mr_obj.References):
                            shape = FreeCAD.ActiveDocument.getObject(sub[0]).Shape
                            elem = sub[1]
                            if elem.startswith('Solid'):  # getElement doesn't work with solids for some reason
                                elt = shape.Solids[int(elem.lstrip('Solid'))-1]
                            else:
                                elt = shape.getElement(elem)
                            if elt.ShapeType == 'Face' or elt.ShapeType == 'Solid':
                                facemesh = MeshPart.meshFromShape(elt,
                                                                  LinearDeflection=self.mesh_obj.STLLinearDeflection)

                                tri_surface += "solid {}{}{}\n".format(mr_obj.Name, sub[0], elem)
                                for face in facemesh.Facets:
                                    tri_surface += " facet normal 0 0 0\n"
                                    tri_surface += "  outer loop\n"
                                    for i in range(3):
                                        p = [i * self.scale for i in face.Points[i]]
                                        tri_surface += "    vertex {} {} {}\n".format(p[0], p[1], p[2])
                                    tri_surface += "  endloop\n"
                                    tri_surface += " endfacet\n"
                                tri_surface += "endsolid {}{}{}\n".format(mr_obj.Name, sub[0], elem)

                                if self.mesh_obj.MeshUtility == 'snappyHexMesh' and mr_obj.Baffle:
                                    # Save baffle references or faces individually
                                    baffle = "{}{}{}".format(mr_obj.Name, sub[0], elem)
                                    fid = open(os.path.join(self.triSurfaceDir, baffle + ".stl"), 'w')
                                    fid.write(tri_surface)
                                    fid.close()
                                    tri_surface = ""
                                    snappy_mesh_region_list.append(baffle)

                        if self.mesh_obj.MeshUtility == 'cfMesh' or not mr_obj.Baffle:
                            fid = open(os.path.join(self.triSurfaceDir, mr_obj.Name + '.stl'), 'w')
                            fid.write(tri_surface)
                            fid.close()

                        if self.mesh_obj.MeshUtility == 'cfMesh' and mr_obj.NumberLayers > 1 and not Internal:
                            for (i, mf) in enumerate(bl_matched_faces):
                                for j in range(len(mf)):
                                    if mr_id == mf[j][0]:
                                        sfN = self.mesh_obj.ShapeFaceNames[i]
                                        ele_meshpatch_map[mr_obj.Name].append(sfN)
                                        patch_list.append(sfN)

                                        # Limit expansion ratio to greater than 1.0 and less than 1.2
                                        expratio = mr_obj.ExpansionRatio
                                        expratio = min(1.2, max(1.0, expratio))

                                        cf_settings['BoundaryLayers'][self.mesh_obj.ShapeFaceNames[i]] = {
                                            'NumberLayers': mr_obj.NumberLayers,
                                            'ExpansionRatio': expratio,
                                            'FirstLayerHeight': self.scale *
                                                                Units.Quantity(mr_obj.FirstLayerHeight).Value
                                        }

                        if self.mesh_obj.MeshUtility == 'cfMesh':
                            if not Internal:
                                cf_settings['MeshRegions'][mr_obj.Name] = {
                                    'RelativeLength': mr_rellen * self.clmax * self.scale,
                                    'RefinementThickness': self.scale * Units.Quantity(
                                        mr_obj.RefinementThickness).Value,
                                }
                            else:
                                cf_settings['InternalRegions'][mr_obj.Name] = {
                                    'RelativeLength': mr_rellen * self.clmax * self.scale
                                }

                        elif self.mesh_obj.MeshUtility == 'snappyHexMesh':
                            refinement_level = CfdTools.relLenToRefinementLevel(mr_obj.RelativeLength)
                            if not Internal:
                                if not mr_obj.Baffle:
                                    snappy_mesh_region_list.append(mr_obj.Name)
                                edge_level = CfdTools.relLenToRefinementLevel(mr_obj.RegionEdgeRefinement)
                                for rL in range(len(snappy_mesh_region_list)):
                                    mrName = mr_obj.Name + snappy_mesh_region_list[rL]
                                    snappy_settings['MeshRegions'][mrName] = {
                                        'RegionName': snappy_mesh_region_list[rL],
                                        'RefinementLevel': refinement_level,
                                        'EdgeRefinementLevel': edge_level,
                                        'MaxRefinementLevel': max(refinement_level, edge_level),
                                        'Baffle': mr_obj.Baffle
                                }
                            else:
                                snappy_settings['InternalRegions'][mr_obj.Name] = {
                                    'RefinementLevel': refinement_level
                                }

                    else:
                        FreeCAD.Console.PrintError(
                            "The meshregion: " + mr_obj.Name + " is not used to create the mesh because the "
                            "CharacteristicLength is 0.0 mm or the reference list is empty.\n")

    def automaticInsidePointDetect(self):
        # Snappy requires that the chosen internal point must remain internal during the meshing process and therefore
        # the meshing algorithm might fail if the point accidentally in a sliver fall between the mesh and the geometry.
        # As a safety measure, the check distance is chosen to be approximately the size of the background mesh.
        shape = self.part_obj.Shape
        step_size = self.clmax*2.5

        bound_box = self.part_obj.Shape.BoundBox
        error_safety_factor = 2.0
        if (step_size*error_safety_factor >= bound_box.XLength or
                        step_size*error_safety_factor >= bound_box.YLength or
                        step_size*error_safety_factor >= bound_box.ZLength):
            CfdTools.cfdError("Current choice in characteristic length of {} might be too large for automatic "
                              "internal point detection.".format(self.clmax))
        x1 = bound_box.XMin
        x2 = bound_box.XMax
        y1 = bound_box.YMin
        y2 = bound_box.YMax
        z1 = bound_box.ZMin
        z2 = bound_box.ZMax
        import random
        while 1:
            x = random.uniform(x1,x2)
            y = random.uniform(y1,y2)
            z = random.uniform(z1,z2)
            pointCheck = FreeCAD.Vector(x,y,z)
            result = shape.isInside(pointCheck,step_size,False)
            if result:
                return pointCheck

    def writePartFile(self):
        """ Construct multi-element STL based on mesh part faces. """
        if self.mesh_obj.MeshUtility == "gmsh":
            self.part_obj.Shape.exportBrep(self.temp_file_shape)
        else:
            if ("Boolean" in self.part_obj.Name) and self.mesh_obj.MeshUtility:
                FreeCAD.Console.PrintError('cfMesh and snappyHexMesh do not accept boolean fragments.')

            with open(self.temp_file_geo, 'w') as fullMeshFile:
                for (i, objFaces) in enumerate(self.part_obj.Shape.Faces):
                    faceName = ("face{}".format(i))
                    mesh_stl = MeshPart.meshFromShape(objFaces, LinearDeflection=self.mesh_obj.STLLinearDeflection)
                    fullMeshFile.write("solid {}\n".format(faceName))
                    for face in mesh_stl.Facets:
                        n = face.Normal
                        fullMeshFile.write(" facet normal {} {} {}\n".format(n[0], n[1], n[2]))
                        fullMeshFile.write("  outer loop\n")
                        for j in range(3):
                            p = face.Points[j]
                            fullMeshFile.write("    vertex {} {} {}".format(self.scale*p[0],
                                                                            self.scale*p[1],
                                                                            self.scale*p[2]))
                            fullMeshFile.write("\n")
                        fullMeshFile.write("  endloop\n")
                        fullMeshFile.write(" endfacet\n")
                    fullMeshFile.write("endsolid {}\n".format(faceName))

    def loadSurfMesh(self):
        if not self.error:
            # NOTE: FemMesh does not support multi element stl
            # fem_mesh = Fem.read(os.path.join(self.meshCaseDir,'mesh_outside.stl'))
            # This is a temp work around to remove multiple solids, but is not very efficient
            import Mesh
            stl = os.path.join(self.meshCaseDir, 'mesh_outside.stl')
            ast = os.path.join(self.meshCaseDir, 'mesh_outside.ast')
            mesh = Mesh.Mesh(stl)
            mesh.write(ast)
            os.remove(stl)
            os.rename(ast, stl)
            fem_mesh = Fem.read(stl)
            fem_mesh_obj = FreeCAD.ActiveDocument.addObject("Fem::FemMeshObject", self.mesh_obj.Name+"_Surf_Vis")
            fem_mesh_obj.FemMesh = fem_mesh
            self.mesh_obj.addObject(fem_mesh_obj)
            print('  Finished loading mesh.')
        else:
            print('No mesh was created.')

    def writeMeshCase(self):
        """ Collect case settings, and finally build a runnable case. """
        FreeCAD.Console.PrintMessage("Populating mesh dictionaries in folder {}\n".format(self.meshCaseDir))

        if self.mesh_obj.MeshUtility == "cfMesh":
            self.cf_settings['ClMax'] = self.clmax*self.scale

            if len(self.cf_settings['BoundaryLayers']) > 0:
                self.cf_settings['BoundaryLayerPresent'] = True
            else:
                self.cf_settings['BoundaryLayerPresent'] = False
            if len(self.cf_settings["InternalRegions"]) > 0:
                self.cf_settings['InternalRefinementRegionsPresent'] = True
            else:
                self.cf_settings['InternalRefinementRegionsPresent'] = False

        elif self.mesh_obj.MeshUtility == "snappyHexMesh":
            bound_box = self.part_obj.Shape.BoundBox
            bC = 5  # Number of background mesh buffer cells
            x_min = (bound_box.XMin - bC*self.clmax)*self.scale
            x_max = (bound_box.XMax + bC*self.clmax)*self.scale
            y_min = (bound_box.YMin - bC*self.clmax)*self.scale
            y_max = (bound_box.YMax + bC*self.clmax)*self.scale
            z_min = (bound_box.ZMin - bC*self.clmax)*self.scale
            z_max = (bound_box.ZMax + bC*self.clmax)*self.scale
            cells_x = int(math.ceil(bound_box.XLength/self.clmax) + 2*bC)
            cells_y = int(math.ceil(bound_box.YLength/self.clmax) + 2*bC)
            cells_z = int(math.ceil(bound_box.ZLength/self.clmax) + 2*bC)

            snappy_settings = self.snappy_settings
            snappy_settings['BlockMesh'] = {
                "xMin": x_min,
                "xMax": x_max,
                "yMin": y_min,
                "yMax": y_max,
                "zMin": z_min,
                "zMax": z_max,
                "cellsX": cells_x,
                "cellsY": cells_y,
                "cellsZ": cells_z
            }

            inside_x = Units.Quantity(self.mesh_obj.PointInMesh.get('x')).Value*self.scale
            inside_y = Units.Quantity(self.mesh_obj.PointInMesh.get('y')).Value*self.scale
            inside_z = Units.Quantity(self.mesh_obj.PointInMesh.get('z')).Value*self.scale

            shape_face_names_list = []
            for i in self.mesh_obj.ShapeFaceNames:
                shape_face_names_list.append(i)
            snappy_settings['ShapeFaceNames'] = tuple(shape_face_names_list)
            snappy_settings['EdgeRefinementLevel'] = CfdTools.relLenToRefinementLevel(self.mesh_obj.EdgeRefinement)
            snappy_settings['PointInMesh'] = {
                "x": inside_x,
                "y": inside_y,
                "z": inside_z
            }
            snappy_settings['CellsBetweenLevels'] = self.mesh_obj.CellsBetweenLevels
            if self.mesh_obj.NumberCores <= 1:
                self.mesh_obj.NumberCores = 1
                snappy_settings['ParallelMesh'] = False
            else:
                snappy_settings['ParallelMesh'] = True
            snappy_settings['NumberCores'] = self.mesh_obj.NumberCores

            if len(self.snappy_settings["InternalRegions"]) > 0:
                self.snappy_settings['InternalRefinementRegionsPresent'] = True
            else:
                self.snappy_settings['InternalRefinementRegionsPresent'] = False
        elif self.mesh_obj.MeshUtility == "gmsh":
            if platform.system() == "Windows":
                exe = os.path.join(FreeCAD.getHomePath(), 'bin', 'gmsh.exe')
            else:
                exe = subprocess.check_output(["which", "gmsh"], universal_newlines=True).rstrip('\n')
            self.gmsh_settings['Executable'] = CfdTools.translatePath(exe)
            self.gmsh_settings['ShapeFile'] = self.temp_file_shape
            self.gmsh_settings['HasLengthMap'] = False
            if self.ele_length_map:
                self.gmsh_settings['HasLengthMap'] = True
                self.gmsh_settings['LengthMap'] = self.ele_length_map
                self.gmsh_settings['NodeMap'] = {}
                for e in self.ele_length_map:
                    ele_nodes = (''.join((str(n+1) + ', ') for n in self.ele_node_map[e])).rstrip(', ')
                    self.gmsh_settings['NodeMap'][e] = ele_nodes
            self.gmsh_settings['ClMax'] = self.clmax
            self.gmsh_settings['ClMin'] = self.clmin
            sols = (''.join((str(n+1) + ', ') for n in range(len(self.mesh_obj.Part.Shape.Solids)))).rstrip(', ')
            self.gmsh_settings['Solids'] = sols
            self.gmsh_settings['BoundaryFaceMap'] = {}
            # Write one boundary per face
            for i in range(len(self.mesh_obj.Part.Shape.Faces)):
                self.gmsh_settings['BoundaryFaceMap']['face'+str(i)] = i+1
            self.gmsh_settings['MeshFile'] = self.temp_file_mesh

        # Perform initialisation here rather than __init__ in case of path changes
        self.template_path = os.path.join(CfdTools.get_module_path(), "data", "defaultsMesh")

        mesh_region_present = False
        if self.mesh_obj.MeshUtility == "cfMesh" and len(self.cf_settings['MeshRegions']) > 0 or \
           self.mesh_obj.MeshUtility == "snappyHexMesh" and len(self.snappy_settings['MeshRegions']) > 0:
            mesh_region_present = True

        self.settings = {
            'Name': self.part_obj.Name,
            'MeshPath': self.meshCaseDir,
            'FoamRuntime': CfdTools.getFoamRuntime(),
            'TranslatedFoamPath': CfdTools.translatePath(CfdTools.getFoamDir()),
            'MeshUtility': self.mesh_obj.MeshUtility,
            'MeshRegionPresent': mesh_region_present,
            'CfSettings': self.cf_settings,
            'SnappySettings': self.snappy_settings,
            'GmshSettings': self.gmsh_settings,
            'TwoDSettings': self.two_d_settings
        }

        TemplateBuilder.TemplateBuilder(self.meshCaseDir, self.template_path, self.settings)

        # Update Allmesh permission - will fail silently on Windows
        fname = os.path.join(self.meshCaseDir, "Allmesh")
        import stat
        s = os.stat(fname)
        os.chmod(fname, s.st_mode | stat.S_IEXEC)

        FreeCAD.Console.PrintMessage("Successfully wrote meshCase to folder {}\n".format(self.meshCaseDir))
