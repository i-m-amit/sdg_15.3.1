import ee 

from scripts import parameter as pm
from scripts import productivity as prod

ee.Initialize()

def land_cover(io, aoi_io, output):
    """Calculate land cover indicator"""

    ## load the land cover map
    lc = ee.Image(pm.land_cover)
    lc = lc \
        .where(lc.eq(9999), -32768) \
        .updateMask(lc.neq(-32768))

    # Remap LC according to input matrix
    lc_remapped = lc \
        .select(f'y{io.start}') \
        .remap(io.transition_matrix[0], io.transition_matrix[1])
    
    for year in range(io.start + 1, io.end + 1):
        lc_remapped = lc_remapped \
            .addBands(lc.select(f'y{year}')) \
            .remap(io.transition_matrix[0], io.transition_matrix[1])

    ## target land cover map reclassified to IPCC 6 classes
    lc_bl = lc_remapped.select(0)

    ## baseline land cover map reclassified to IPCC 6 classes
    lc_tg = lc_remapped.select(len(lc_remapped.getInfo()['bands']) - 1)

    ## compute transition map (first digit for baseline land cover, and second digit for target year land cover)
    lc_tr = lc_bl.multiply(10).add(lc_tg)

    ## definition of land cover transitions as degradation (-1), improvement (1), or no relevant change (0)
    
    lc_dg = lc_tr.remap(pm.IPCC_matrix,io.transition_matrix)

    ## Remap persistence classes so they are sequential.
    
    sequential_matrix = [
        1, 12, 13, 14, 15, 16, 17,
        21, 2, 23, 24, 25, 26, 27,
        31, 32, 3, 34, 35, 36, 37,
        41, 42, 43, 4, 45, 46, 47,
        51, 52, 53, 54, 5, 56, 57,
        61, 62, 63, 64, 65, 6, 67,
        71, 72, 73, 74, 75, 76, 7
    ]
    
    lc_tr = lc_tr.remap(pm.IPCC_matrix, sequential_matrix)

    out = ee.Image(
        lc_dg \
        .addBands(lc.select(f'y{io.start}')) \
        .addBands(lc.select(f'y{io.target_start}')) \
        .addBands(lc_tr)
    )

    # Return the full land cover timeseries so it is available for reporting
    out.addBands(lc_remapped)

    out= out.unmask(-32768).int16()

    return out

def integrate_ndvi_climate(aoi_io, io, output):
    
    # create the composite image collection
    i_img_coll = ee.ImageCollection([])
    
    for sensor in io.sensors:
        # get the featureCollection 
        sat = ee.FeatureCollection(pm.sensors[sensor])
        # rename the bands 
        sat = sat.map(partial(u.rename_band, sensor=sensor))
        # mask the clouds 
        sat = sat.map(partial(u.cloud_mask, sensor=sensor))
    
        i_img_coll = i_img_coll.merge(sat)
    
    # Filtering the img collection  using start year and end year and filtering to the bb area of interest
    i_img_coll = i_img_coll.filterDate(io.start, io.end).filterBounds(aoi_io.get_aoi_ee())

    # Function to integrate observed NDVI datasets at the annual level
    ndvi_coll = i_img_coll.map(prod.CalcNDVI)
    
    ndvi_int = prod.int_yearly_ndvi(ndvi_coll, io.start, io.end)

    # get the trends
    trend = ndvi_trend(io.start, io.end, ndvi_int)

    # process the climate dataset to use with the pixel restrend, RUE calculation
    precipitation = ee.ImageCollection(pm.precipitation) \
        .filterDate(io.start,io.end) \
        .select('precipitation')
    
    climate_int = prod.int_yearly_climate(precipitation, io.start, io.end)
    
    return (ndvi_int, climate_int)

def productivity_trajectory(io, nvdi_yearly_integration, climate_yearly_integration, output):
    """Productivity Trend describes the trajectory of change in productivity over time. Trend is calculated by fitting a robust, non-parametric linear regression model.The significance of trajectory slopes at the P <= 0.05 level should be reported in terms of three classes:
        1) Z score < -1.96 = Potential degradation, as indicated by a significant decreasing trend,
        2) Z score > 1.96 = Potential improvement, as indicated by a significant increasing trend, or
        3) Z score > -1.96 AND < 1.96 = No significant change

In order to correct the effects of climate on productivity, climate adjusted trend analysis can be performed. There such methods are coded for the trajectory analysis. 

The following code runs the selected trend method and produce an output by reclassifying the trajecry slopes. 
    """
    
    trajectories = ['ndvi_trend', 'p_restrend', 's_restrend', 'ue_trend']

    # Run the selected algorithm
    # nvi trend
    if io.trajectory == pm.trajectories[0]:
        lf_trend, mk_trend = prod.ndvi_trend(io.start, io.end, nvdi_yearly_integration)
    # p restrend
    elif io.trajectory == pm.trajectories[1]:
        ###################################
        # why would it be null ????
        if climate_1yr == None:
            climate_1yr = precp_gpcc
        ####################################
        lf_trend, mk_trend = prod.p_restrend(io.start, io.end, nvdi_yearly_integration, climate_yearly_integration)
    # s restrend
    elif io.trajectory == pm.trajectories[2]:
        #TODO: need to code this
        raise NameError("s_restrend method not yet supported")
    # ue trend
    elif io.trajectory == pm.trajectories[3]:
        lf_trend, mk_trend = prod.ue_trend(io.start, io.end, nvdi_yearly_integration, climate_yearly_integration)
    else:
        raise NameError(f'Unrecognized method "{io.trajectory}"')

    # Define Kendall parameter values for a significance of 0.05
    period = io.start - io.end + 1
    kendall90 = pm.get_kendall_coef(period, 90)
    kendall95 = pm.get_kendall_coef(period, 95)
    kendall99 = pm.get_kendall_coef(period, 99)

    # Create final productivity trajectory output layer. Positive values are 
    # significant increase, negative values are significant decrease.
    signif = ee.Image(-32768) \
        .where(lf_trend.select('scale').gt(0).And(mk_trend.abs().gte(kendall90)), 1) \
        .where(lf_trend.select('scale').gt(0).And(mk_trend.abs().gte(kendall95)), 2) \
        .where(lf_trend.select('scale').gt(0).And(mk_trend.abs().gte(kendall99)), 3) \
        .where(lf_trend.select('scale').lt(0).And(mk_trend.abs().gte(kendall90)), -1) \
        .where(lf_trend.select('scale').lt(0).And(mk_trend.abs().gte(kendall95)), -2) \
        .where(lf_trend.select('scale').lt(0).And(mk_trend.abs().gte(kendall99)), -3) \
        .where(mk_trend.abs().lte(kendall90), 0) \
        .where(lf_trend.select('scale').abs().lte(10), 0).rename('signif')

    out = ee.Image(
        lf_trend.select('scale') \
        .addBands(signif) \
        .addBands(mk_trend) \
        .unmask(-32768) \
        .int16()
    )
    
    return out

def productivity_performance(io_aoi, io, nvdi_yearly_integration, climate_yearly_integration, output):
    """It measures local productivity relative to other similar vegetation types in similar land cover types and bioclimatic regions. It indicates how a region is performing relative to other regions with similar productivity potential.
        Steps:
        *Computation of mean NDVI for the analysis period,
        *Creation of ecologically similar regions based on USDA taxonomy and ESA CCI land cover data sets.
        *Extraction of mean NDVI for each region, creation of  a frequency distribution of this data to determine the value that represents 90th percentile,
        *Computation of the ratio of mean NDVI and max productivity (90th percentile)

    """
    
    #year_start, year_end, ndvi_1yr, AOI

    nvdi_yearly_integration = ee.Image(nvdi_yearly_integration)

    # land cover data from esa cci
    lc = ee.Image(pm.land_cover)
    lc = lc \
        .where(lc.eq(9999), -32768) \
        .updateMask(lc.neq(-32768))

    # global agroecological zones from IIASA
    soil_tax_usda = ee.Image(pm.soil_tax)

    ###############################################
    # why clipping twice ??
    
    # Make sure the bounding box of the poly is used, and not the geodesic 
    # version, for the clipping
    poly = io_aoi.get_aoi_ee().geometry(geodesics=False)
    #############################################

    # compute mean ndvi for the period
    years = ee.List([f'y{year}' for year in range(io.start, io.end + 1)])
    
    ndvi_avg = nvdi_yearly_integration \
        .select(years) \
        .reduce(ee.Reducer.mean()) \
        .rename(['ndvi']) \
        .clip(poly)

    ################################
    
    # should not be here it's a hidden parameter
    
    # Handle case of year_start that isn't included in the CCI data
    lc_year_start = min(max(io.start, pm.lc_first_year), pm.ls_last_year)
    
    #################################
    
    # reclassify lc to ipcc classes
    lc_t0 = lc.select('y{}'.format(lc_year_start)) \
        .remap([10, 11, 12, 20 , 30, 40, 50, 60, 61, 62, 70, 71, 72, 80, 81, 82, 90, 100, 160, 170, 110, 130, 180, 190, 120, 121, 122, 140, 150, 151, 152, 153, 200, 201, 202, 210], 
               [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36])

    # create a binary mask.
    mask = ndvi_avg.neq(0)

    # define projection attributes
    ndvi_proj = nvdi_yearly_integration.projection()

    # reproject land cover, soil_tax_usda and avhrr to modis resolution
    lc_proj = lc_t0.reproject(crs=ndvi_proj)
    soil_tax_usda_proj = soil_tax_usda.reproject(crs=ndvi_proj)
    ndvi_avg_proj = ndvi_avg.reproject(crs=ndvi_proj)

    # define unit of analysis as the intersect of soil_tax_usda and land cover
    units = soil_tax_usda_proj.multiply(100).add(lc_proj)

    # create a 2 band raster to compute 90th percentile per unit (analysis restricted by mask and study area)
    ndvi_id = ndvi_avg_proj.addBands(units).updateMask(mask)

    # compute 90th percentile by unit
    perc90 = ndvi_id.reduceRegion(
        reducer=ee.Reducer.percentile([90]).group(
            groupField=1, 
            groupName='code'
        ),
        geometry=poly,
        scale=ee.Number(modis_proj.nominalScale()).getInfo(),
        maxPixels=1e15
    )

    # Extract the cluster IDs and the 90th percentile
    groups = ee.List(perc90.get("groups"))
    ids = groups.map(lambda d: ee.Dictionary(d).get('code'))
    perc = groups.map(lambda d: ee.Dictionary(d).get('p90'))

    # remap the units raster using their 90th percentile value
    raster_perc = units.remap(ids, perc)

    # compute the ration of observed ndvi to 90th for that class
    obs_ratio = ndvi_avg_proj.divide(raster_perc)

    # aggregate obs_ratio to original NDVI data resolution (for modis this step does not change anything)
    obs_ratio_2 = obs_ratio.reduceResolution(reducer=ee.Reducer.mean(), maxPixels=2000) \
        .reproject(crs=ndvi_1yr.projection())

    # create final degradation output layer (9999 is background), 0 is not
    # degreaded, -1 is degraded
    lp_perf_deg = ee.Image(-32768) \
        .where(obs_ratio_2.gte(0.5), 0) \
        .where(obs_ratio_2.lte(0.5), -1)

    output = ee.Image(
        lp_perf_deg.addBands(obs_ratio_2.multiply(10000)) \
        .addBands(units) \
        .unmask(-32768) \
        .int16()
    )
    
    return output

def productivity_state(io_aoi, io, nvdi_yearly_integration, climate_int, output):
    """It represents the level of relative roductivity in a pixel compred to a historical observations of productivity for that pixel. For more, see Ivits, E., & Cherlet, M. (2016). Land productivity dynamics: towards integrated assessment of land degradation at global scales. In. Luxembourg: Joint Research Centr, https://publications.jrc.ec.europa.eu/repository/bitstream/JRC80541/lb-na-26052-en-n%20.pdf
        It alows for the detection of recent changes in primary productivity as compared to the baseline period.
        Steps:
        *Definition of baselene and reporting perod,
        *Computation of frequency distribution of mean NDVI for baseline period with addition of 5% at the both extremes of the distribution to alow inclusion of some, if an, missed extreme values in NDVI.
        *Creation of 10 percentile classess using the data from the frequency distribution.
        *computation of mean NDVI for baseline period, and determination of the percentile class it belongs to. Assignmentof the mean NDVI for the base line period the number corresponding to that percentile class. 
        *computation of mean NDVI for reporting period, and determination of the percentile class it belongs to. Assignmentof the mean NDVI for the reporting period the number corresponding to that percentile class. 
        *Determination of the difference in class number between the reporting and baseline period,
        *
    """
    
    #year_bl_start, year_bl_end, year_tg_start, year_tg_end, nvdi_yearly_integration
    
    
    ############################
    
    # why do we need to use ee.Image ?
    nvdi_yearly_integration = ee.Image(nvdi_yearly_integration)
    
    ############################

    # compute min and max of annual ndvi for the baseline period
    years = ee.List([f'y{year}' for year in range(year_bl_start, year_bl_end + 1)])
    
    bl_ndvi_range = nvdi_yearly_integration \
        .select(years) \
        .reduce(ee.Reducer.percentile([0, 100]))

    # add two bands to the time series: one 5% lower than min and one 5% higher than max
    
    ##############################
    
    # this var needs to have an explicit name
    name_this_var = (bl_ndvi_range.select('p100').subtract(bl_ndvi_range.select('p0'))).multiply(0.05)
    
    #############################
    
    bl_ndvi_ext = nvdi_yearly_integration \
        .select(years) \
        .addBands(
            bl_ndvi_range \
            .select('p0') \
            .subtract(name_this_var)
        ) \
        .addBands(
            bl_ndvi_range \
            .select('p100') \
            .add(name_this_var)
        )

    # compute percentiles of annual ndvi for the extended baseline period
    percentiles = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    bl_ndvi_perc = bl_ndvi_ext.reduce(ee.Reducer.percentile(percentiles))

    # compute mean ndvi for the baseline and target period period
    bl_ndvi_mean = nvdi_yearly_integration \
        .select(years) \
        .reduce(ee.Reducer.mean()) \
        .rename(['ndvi'])
    
    tg_ndvi_mean = nvdi_yearly_integration \
        .select(years) \
        .reduce(ee.Reducer.mean()) \
        .rename(['ndvi'])

    # reclassify mean ndvi for baseline period based on the percentiles
    bl_classes = ee.Image(-32768) \
        .where(bl_ndvi_mean.lte(bl_ndvi_perc.select('p10')), 1) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p10')), 2) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p20')), 3) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p30')), 4) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p40')), 5) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p50')), 6) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p60')), 7) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p70')), 8) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p80')), 9) \
        .where(bl_ndvi_mean.gt(bl_ndvi_perc.select('p90')), 10)

    # reclassify mean ndvi for target period based on the percentiles
    tg_classes = ee.Image(-32768) \
        .where(tg_ndvi_mean.lte(bl_ndvi_perc.select('p10')), 1) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p10')), 2) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p20')), 3) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p30')), 4) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p40')), 5) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p50')), 6) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p60')), 7) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p70')), 8) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p80')), 9) \
        .where(tg_ndvi_mean.gt(bl_ndvi_perc.select('p90')), 10)

    # difference between start and end clusters >= 2 means improvement (<= -2 
    # is degradation)
    classes_chg = tg_classes.subtract(bl_classes).where(bl_ndvi_mean.subtract(tg_ndvi_mean).abs().lte(100), 0)

    out = ee.Image(
        lasses_chg \
        .addBands(bl_classes) \
        .addBands(tg_classes) \
        .addBands(bl_ndvi_mean) \
        .addBands(tg_ndvi_mean) \
        .int16()
    )
    
    return out

# TODO need to combile the results from the three function to get the final out put
